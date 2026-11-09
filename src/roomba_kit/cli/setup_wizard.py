"""`roomba-kit setup`: the guided setup wizard (FR-001–FR-008, T046).

Presentation only (Constitution III): every change goes through a registry operation on the
service. The one local step is minting the `cli` API token on first run, which is the
`token_create` local file op, since nothing can call the service before that token exists.
"""

import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import click

from roomba_kit.cli import lifecycle
from roomba_kit.cli.client import CliState, ServiceClient, fail
from roomba_kit.cli.generate import group_for, read_secret
from roomba_kit.cli.generate import run as run_op
from roomba_kit.config import tokens
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.core import registry
from roomba_kit.core.errors import CLI_EXIT, ErrorCode
from roomba_kit.lifecycle import daemon
from roomba_kit.service.operations.setup import PASSWORD_STEPS

RETRIEVE_TIMEOUT_S = 60
VPN_HINT = (
    "Nothing answered. If a VPN is on, it may own the network route: broadcast to your own "
    "subnet instead (e.g. 192.0.2.255), or give the robot's IP address (your router's client "
    "list shows it)."
)
CLOUD_PITCH = """\
Optional: iRobot account credentials.
They add room names and IDs, floor plans, cloud mission history, coverage maps, the live
mission timeline, and password retrieval for robots that won't release it locally.
Every local feature (cleaning, docking, state, settings, events) works without them.
They're stored in credentials.toml (owner-only) and checked by logging in first."""


class OpFailed(Exception):
    def __init__(self, error: dict[str, Any]) -> None:
        super().__init__(error.get("message", ""))
        self.code = str(error.get("code", "internal"))
        self.message = str(error.get("message", ""))


@contextmanager
def countdown(seconds: int) -> Iterator[None]:
    """Show the seconds left on a terminal while a blocking call runs."""
    if not sys.stderr.isatty():
        yield
        return
    done = threading.Event()
    end = time.monotonic() + seconds

    def tick() -> None:
        while not done.wait(1):
            left = max(0, int(end - time.monotonic()))
            click.echo(f"\r   {left:3d} s left ", err=True, nl=False)

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join()
        click.echo("\r" + " " * 20 + "\r", err=True, nl=False)


def _robot_line(r: dict[str, Any]) -> str:
    model = r.get("profile_id") or "unsupported model"
    return f"{r['name']}  ({model}, SKU {r['sku']}, {r['address']}, ID {r['id']})"


class Wizard:
    def __init__(self, client: ServiceClient) -> None:
        self.client = client
        self.cloud_only: list[dict[str, Any]] = []
        self.added_by_ip: set[str] = set()

    def call(self, name: str, **params: Any) -> Any:
        response = self.client.call(registry.get(name), params)
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            raise OpFailed(body["error"])
        if response.is_error:
            raise OpFailed({"code": "internal", "message": f"HTTP {response.status_code}"})
        return body

    def attempt(self, name: str, **params: Any) -> Any:
        """`call`, printing a failure instead of raising it. None on failure."""
        try:
            return self.call(name, **params)
        except OpFailed as err:
            click.secho(f"  ✗ {err.message} ({err.code})", fg="red")
            return None

    # -- steps -----------------------------------------------------------------------------------

    def discover(self, broadcast: str | None) -> list[dict[str, Any]]:
        click.echo("\nLooking for robots on your network…")
        found: list[dict[str, Any]] = self.attempt("discover_robots", broadcast=broadcast) or []
        while not found:
            click.echo(VPN_HINT)
            address = click.prompt(
                "Broadcast address or robot IP (blank to skip)", default="", show_default=False
            ).strip()
            if not address:
                return []
            found = self.attempt("discover_robots", broadcast=address) or []
            if not found and (added := self.attempt("add_robot", address=address)):
                click.echo(f"  Added {_robot_line(added)}")
                self.added_by_ip.add(added["id"])
                found = [added]
        return found

    def add_robots(self, found: list[dict[str, Any]]) -> list[dict[str, Any]]:
        added = []
        for robot in found:
            if robot["id"] in self.added_by_ip:
                added.append(robot)
                continue
            if robot.get("configured"):
                click.echo(f"  • {_robot_line(robot)} — already set up")
                continue
            click.echo(f"  • {_robot_line(robot)}")
            if not robot["supported"]:
                click.echo("    This model has no profile: it can be listed but not controlled.")
            if click.confirm("    Add it?", default=bool(robot["supported"])):
                if self.attempt("add_robot", address=robot["address"]) is not None:
                    added.append(robot)
        return added

    def pair(self, robot: dict[str, Any]) -> None:
        name = robot["name"]
        while True:
            first, *rest = PASSWORD_STEPS
            click.echo(f"\nPairing {name}. At the robot:\n  1. {first}")
            click.prompt("     Press Enter when it's there", default="", show_default=False)
            for i, step in enumerate(rest, 2):
                click.echo(f"  {i}. {step}")
            click.echo(f"     (retrying for up to {RETRIEVE_TIMEOUT_S} s)")
            with countdown(RETRIEVE_TIMEOUT_S):
                result = self.attempt(
                    "retrieve_password", robot=robot["id"], timeout_s=RETRIEVE_TIMEOUT_S
                )
            outcome = result and result["outcome"]
            if outcome == "stored":
                click.secho(f"  ✓ {name}: password retrieved, verified and stored.", fg="green")
                return
            if outcome == "cloud_only":
                click.echo(
                    f"  {name} didn't release its password locally. Hold HOME until it chimes "
                    "and retry; some robots only give it to the iRobot account, which you can "
                    "use below."
                )
            elif outcome == "timeout":
                click.echo(f"  {name} didn't answer. Check it's on its dock and on Wi-Fi.")
            choice = click.prompt(
                "  [r]etry, enter the password [m]anually, or [s]kip",
                type=click.Choice(["r", "m", "s"]),
                default="r",
            )
            if choice == "m":
                password = click.prompt("  Robot password", hide_input=True)
                stored = self.attempt("set_robot_password", robot=robot["id"], password=password)
                if stored and stored["outcome"] == "stored":
                    click.secho(f"  ✓ {name}: password verified and stored.", fg="green")
                    return
                click.echo("  The robot rejected that password.")
            elif choice == "s":
                if outcome == "cloud_only":
                    self.cloud_only.append(robot)
                return

    def cloud(self) -> bool:
        """Offer account credentials. True when the cloud is connected afterwards."""
        status = self.attempt("cloud_status") or {}
        click.echo("\n" + CLOUD_PITCH)
        if status.get("status") == "connected":
            click.echo("The iRobot account is already connected.")
            if not click.confirm("Change it?", default=False):
                return True
        elif not click.confirm("Add iRobot account credentials?", default=False):
            click.echo("Skipped. Cloud features will report `cloud_unavailable` (not_configured).")
            return False
        while True:
            username = click.prompt("iRobot account email")
            password = click.prompt("iRobot account password", hide_input=True)
            country = click.prompt("Account country code", default="US")
            result = self.attempt(
                "cloud_login", username=username, password=password, country=country
            )
            if result and result["status"] == "connected":
                click.secho("  ✓ Logged in; credentials stored.", fg="green")
                return True
            if result:
                click.echo(f"  Login failed ({result.get('reason')}); nothing was saved.")
            if not click.confirm("Try again?", default=True):
                return False

    def cloud_passwords(self) -> None:
        for robot in self.cloud_only:
            if not click.confirm(f"Get {robot['name']}'s password from the account?", default=True):
                continue
            result = self.attempt("cloud_retrieve_password", robot=robot["id"])
            if result and result["outcome"] == "stored":
                click.secho(f"  ✓ {robot['name']}: password stored and verified.", fg="green")
            elif result:
                click.echo(f"  Not available ({result.get('reason')}).")

    def import_rooms(self, robots: list[dict[str, Any]], *, asked: bool = False) -> None:
        if "import_rooms" not in {op.name for op in registry.all()}:
            if asked:
                click.echo("Room import isn't available in this version.")
            return
        for robot in robots:
            if robot.get("supported"):
                result = self.attempt("import_rooms", robot=robot["id"])
                if result is not None:
                    click.echo(f"  {robot['name']}: imported {result.get('imported', 0)} rooms.")

    def choose_robot(self, robots: list[dict[str, Any]]) -> dict[str, Any]:
        for i, robot in enumerate(robots, 1):
            click.echo(f"  {i}. {_robot_line(robot)}")
        index = click.prompt("Robot", type=click.IntRange(1, len(robots)))
        return robots[index - 1]

    # -- flows -----------------------------------------------------------------------------------

    def first_run(self, broadcast: str | None) -> None:
        added = self.add_robots(self.discover(broadcast))
        for robot in added:
            if robot["supported"]:
                self.pair(robot)
        if self.cloud():
            self.cloud_passwords()
            self.import_rooms(added)
        self.next_steps(added)

    def menu(self, broadcast: str | None) -> None:
        options = [
            "Add a robot",
            "Re-pair a robot (retrieve or set its password)",
            "Add, change or remove iRobot account credentials",
            "Re-import rooms",
            "Remove a robot",
            "Done",
        ]
        while True:
            robots: list[dict[str, Any]] = self.call("list_robots")
            click.echo("\nConfigured robots:")
            for robot in robots:
                click.echo(f"  • {_robot_line(robot)} — {robot['connection']}")
            for i, text in enumerate(options, 1):
                click.echo(f"  {i}. {text}")
            choice = click.prompt("Choose", type=click.IntRange(1, len(options)), default=6)
            if choice == 1:
                for robot in self.add_robots(self.discover(broadcast)):
                    if robot["supported"]:
                        self.pair(robot)
            elif choice == 2 and robots:
                self.pair(self.choose_robot(robots))
            elif choice == 3:
                self.account()
            elif choice == 4 and robots:
                self.import_rooms(robots, asked=True)
            elif choice == 5 and robots:
                robot = self.choose_robot(robots)
                if click.confirm(f"Remove {robot['name']} and its password?", default=False):
                    self.attempt("remove_robot", robot=robot["id"])
            elif choice == 6:
                return

    def account(self) -> None:
        status = self.attempt("cloud_status") or {}
        if status.get("status") != "not_configured" and click.confirm(
            "Remove the stored iRobot account credentials?", default=False
        ):
            self.attempt("cloud_logout")
            return
        if self.cloud():
            self.cloud_passwords()

    def next_steps(self, added: list[dict[str, Any]]) -> None:
        name = added[0]["name"] if added else "<robot>"
        click.echo(
            "\nDone. Try:\n"
            "  roomba-kit robots list\n"
            f'  roomba-kit clean "{name}"\n'
            f'  roomba-kit dock "{name}"\n'
            "Re-run `roomba-kit setup` any time to add, re-pair or remove robots."
        )


def ensure_cli_token() -> None:
    """Mint the `cli` token if this machine has none (first run)."""
    if tokens.read_cli_token() is not None:
        return
    # `replace`: a `cli` record whose plaintext file is gone can never be used again anyway.
    tokens.TokenStore(CredentialStore()).create(tokens.CLI_TOKEN_NAME, replace=True)
    click.echo("Created the `cli` API token for this machine.")


@click.command()
@click.option("--broadcast", metavar="ADDR", help="Discovery broadcast address, e.g. under a VPN.")
@click.pass_context
def setup(ctx: click.Context, broadcast: str | None) -> None:
    """Guided setup: find robots, pair them, and optionally add an iRobot account.

    Re-run it any time to add, re-pair or remove robots, or change the account.
    """
    state: CliState = ctx.obj
    ensure_cli_token()
    if daemon.health(state.url()) is None:
        if not click.confirm("The service isn't running. Start it now?", default=True):
            fail(ErrorCode.SERVICE_UNAVAILABLE, "Setup needs the service: roomba-kit service start")
        ctx.invoke(lifecycle.service_start, timeout=10.0)
    with state.client() as client:
        wizard = Wizard(client)
        try:
            if wizard.call("list_robots"):
                wizard.menu(broadcast)
            else:
                wizard.first_run(broadcast)
        except OpFailed as err:
            click.echo(f"Error ({err.code}): {err.message}", err=True)
            code = ErrorCode(err.code) if err.code in ErrorCode else ErrorCode.INTERNAL
            raise SystemExit(CLI_EXIT[code]) from None


@click.command("password")
@click.argument("robot")
@click.option("--manual", is_flag=True, help="Type the password instead of retrieving it.")
@click.option("--timeout-s", type=float, help="How long to keep retrying (default 60).")
@click.pass_obj
def robots_password(state: CliState, robot: str, manual: bool, timeout_s: float | None) -> None:
    """Pair a robot: retrieve its password while you hold HOME, or --manual to type it.

    Either way it's verified by connecting before it's stored, and never shown.
    """
    if manual:
        params: dict[str, Any] = {"robot": robot, "password": read_secret("password")}
        run_op(state, registry.get("set_robot_password"), params)
        return
    click.echo("\n".join(f"{i}. {step}" for i, step in enumerate(PASSWORD_STEPS, 1)), err=True)
    params = {"robot": robot} | ({"timeout_s": timeout_s} if timeout_s else {})
    run_op(state, registry.get("retrieve_password"), params)


def register(root: click.Group) -> None:
    root.add_command(setup)
    robots = group_for(root, ("robots",))
    robots.help = "Add, list, pair and remove robots."
    names = {op.name for op in registry.all()}
    if {"retrieve_password", "set_robot_password"} <= names:  # absent in isolated test registries
        robots.add_command(robots_password)
    group_for(root, ("cloud",)).help = "The optional iRobot account: login, status, passwords."
