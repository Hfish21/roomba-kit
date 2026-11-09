"""Lifecycle commands, local and working without the service: serve, service, config path, version.

Registered through the `register(root)` seam in `cli/main.py`.
"""

import sys
from typing import Any

import click

from roomba_kit import __version__
from roomba_kit.cli.client import CliState, fail, local_service_url
from roomba_kit.cli.generate import group_for
from roomba_kit.cli.output import render, wire_json
from roomba_kit.config import paths
from roomba_kit.config.settings import load_settings
from roomba_kit.core import registry
from roomba_kit.core.errors import CLI_EXIT, ErrorCode, KitError
from roomba_kit.lifecycle import daemon


def serve_command() -> list[str]:
    """What `service start` spawns. Tests replace it with a fake health server."""
    return [sys.executable, "-m", "roomba_kit.cli.main", "serve"]


def report(state: CliState, data: dict[str, Any], text: str, *, essential: bool = False) -> None:
    if state.json:
        click.echo(wire_json(data))
    elif essential or not state.quiet:
        click.echo(text)


@click.command()
def serve() -> None:
    """Run the service in the foreground on `server.host`:`server.port`."""
    import uvicorn

    try:
        server = load_settings().server
    except KitError as exc:
        fail(exc.code, f"Error ({exc.code.value}): {exc.message}")
    uvicorn.run("roomba_kit.api.app:create_app", factory=True, host=server.host, port=server.port)


@click.command("start")
@click.option("--timeout", default=10.0, show_default=True, help="Seconds to wait for health.")
@click.pass_obj
def service_start(state: CliState, timeout: float) -> None:
    """Start the service in the background; logs go to `<state>/service.log`."""
    url = local_service_url()
    data = {"url": url, "log": str(paths.log_file())}
    if (pid := daemon.running_pid(paths.pid_file())) is not None:
        report(state, {**data, "pid": pid, "started": False}, f"Already running (pid {pid}).")
        return
    if daemon.health(url) is not None:
        text = f"A service is already answering at {url} (not started by `service start`)."
        report(state, {**data, "pid": None, "started": False}, text)
        return
    try:
        pid = daemon.start(
            serve_command(),
            pid_path=paths.pid_file(),
            log_path=paths.log_file(),
            url=url,
            timeout=timeout,
        )
    except daemon.DaemonError as exc:
        fail(ErrorCode.SERVICE_UNAVAILABLE, str(exc))
    text = f"Service started (pid {pid}) at {url}. Logs: {data['log']}"
    report(state, {**data, "pid": pid, "started": True}, text)


@click.command("stop")
@click.option("--timeout", default=10.0, show_default=True, help="Seconds before SIGKILL.")
@click.pass_obj
def service_stop(state: CliState, timeout: float) -> None:
    """Stop the background service: SIGTERM, then SIGKILL after the timeout."""
    try:
        pid = daemon.stop(paths.pid_file(), timeout=timeout)
    except daemon.DaemonError as exc:
        fail(ErrorCode.INTERNAL, str(exc))
    text = "Service not running." if pid is None else f"Service stopped (pid {pid})."
    report(state, {"pid": pid, "stopped": pid is not None}, text)


def _service_status(state: CliState) -> Any:
    try:
        op = registry.get("service_status")
    except KitError:
        return None
    with state.client() as client:
        response = client.call(op, {})
    return response.json() if response.is_success else None


@click.command("status")
@click.pass_obj
def service_status(state: CliState) -> None:
    """Pid, health and `service_status`. Exits 69 when the service isn't answering."""
    url = state.url()
    healthy = daemon.health(url)
    data: dict[str, Any] = {
        "pid": daemon.running_pid(paths.pid_file()),
        "url": url,
        "health": healthy,
        "service_status": _service_status(state) if healthy else None,
    }
    lines = {"pid": data["pid"], "url": url, "health": healthy or "not answering"}
    if isinstance(data["service_status"], dict):
        lines.update(data["service_status"])
    report(state, data, render(lines), essential=True)
    if healthy is None:
        raise SystemExit(CLI_EXIT[ErrorCode.SERVICE_UNAVAILABLE])


@click.command("logs")
@click.option("-n", "--lines", default=50, show_default=True, help="Lines of history to show.")
@click.option("-f", "--follow", is_flag=True, help="Keep printing new lines.")
def service_logs(lines: int, follow: bool) -> None:
    """Show the background service's log."""
    log = paths.log_file()
    if not log.exists():
        click.echo(f"No log yet at {log}.", err=True)
        return
    if text := daemon.tail(log, lines):
        click.echo(text)
    if follow:
        try:
            for line in daemon.follow(log):
                click.echo(line, nl=False)
        except KeyboardInterrupt:
            pass


@click.command("path")
@click.pass_obj
def config_path(state: CliState) -> None:
    """Print the config and state directories."""
    data = {"config_dir": str(paths.config_dir()), "state_dir": str(paths.state_dir())}
    report(state, data, render(data), essential=True)


@click.command()
@click.pass_obj
def version(state: CliState) -> None:
    """Print the kit version."""
    report(state, {"version": __version__}, f"roomba-kit {__version__}", essential=True)


def register(root: click.Group) -> None:
    root.add_command(serve)
    root.add_command(version)
    group_for(root, ("config",)).add_command(config_path)
    service = group_for(root, ("service",))
    service.help = "Run the service in the background and manage it."
    for command in (service_start, service_stop, service_status, service_logs):
        service.add_command(command)
    # `service install` / `service uninstall` (T092) are added here.
