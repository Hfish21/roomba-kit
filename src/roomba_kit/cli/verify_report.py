"""`roomba-kit verify`: the generated command plus a motion prompt and `--write-report` (R16).

`--write-report` appends the run's markdown to `docs/verification/<profile>.md` in the repo
checkout it is run from, so the run can be committed and cited by the profile.
"""

from pathlib import Path
from typing import Any

import click

from roomba_kit.cli.client import CliState, fail
from roomba_kit.cli.generate import command as generated_command
from roomba_kit.cli.generate import group_for, help_text
from roomba_kit.cli.output import NO_TOKEN_HINT, emit_error, render
from roomba_kit.core import registry
from roomba_kit.core.errors import ErrorCode

REPORT_DIR = Path("docs/verification")
MOTION_PROMPT = (
    "This will move the robot (and may empty its bin or reboot it). Make sure it is docked, "
    "charged and has room to drive. Continue?"
)


def _repo_root() -> Path | None:
    """The CWD when it is a roomba-kit checkout, else None."""
    pyproject = Path("pyproject.toml")
    try:
        is_kit = 'name = "roomba-kit"' in pyproject.read_text()
    except OSError:
        return None
    return Path.cwd() if is_kit and Path("docs").is_dir() else None


def write_report(profile_id: str, report: str) -> Path:
    path = REPORT_DIR / f"{profile_id}.md"
    if not path.exists():
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Verification: `{profile_id}`\n\n"
            "Append-only log of `roomba-kit verify` runs on real hardware. A profile cites a "
            "run by its anchor, e.g. `Verification.verified(run="
            f'"docs/verification/{profile_id}.md#run-YYYYMMDD-HHMMSS")`.\n'
        )
    with path.open("a") as out:
        out.write("\n" + report)
    return path


def _verify_command() -> click.Command:
    op = registry.get("verify")
    base = generated_command(op)

    @click.pass_obj
    def callback(state: CliState, /, write_report_: bool, yes: bool, **values: Any) -> None:
        if write_report_ and _repo_root() is None:
            fail(
                ErrorCode.INVALID_REQUEST,
                "--write-report appends to docs/verification/ in the roomba-kit repo: run it "
                "from the repo root.",
            )
        if values.get("allow_motion") and not yes:
            click.confirm(MOTION_PROMPT, abort=True, err=True)
        params = {k: list(v) if isinstance(v, tuple) else v for k, v in values.items()}
        params = {k: v for k, v in params.items() if v not in (None, [])}
        with state.client() as client:
            response = client.call(op, params)
        if response.status_code == 401 and not client.has_token:
            click.echo(NO_TOKEN_HINT, err=True)
        body = response.content
        try:
            value = response.json()
        except ValueError:
            value = None
        if isinstance(value, dict) and isinstance(value.get("error"), dict):
            emit_error(state, body, value["error"])
        if response.is_error or not isinstance(value, dict):
            fail(ErrorCode.INTERNAL, f"Unexpected response: HTTP {response.status_code}")
        click.echo(body if state.json else render(value["results"]))
        if write_report_:
            path = write_report(value["profile_id"], value["report"])
            click.echo(f"Appended to {path}; cite as {path}#{value['anchor']}", err=True)

    return click.Command(
        base.name,
        callback=callback,
        params=[
            *base.params,
            click.Option(
                ["--write-report", "write_report_"],
                is_flag=True,
                help="Append the report to docs/verification/<profile>.md (run from the repo).",
            ),
            click.Option(["--yes", "-y"], is_flag=True, help="Don't prompt before motion checks."),
        ],
        help=help_text(op)
        + "\n\nWith --allow-motion the command asks before moving the robot (skip with --yes).",
    )


def register(root: click.Group) -> None:
    if "verify" in root.commands:
        root.add_command(_verify_command())
    if "settings" in root.commands:
        group_for(root, ("settings",)).help = "Read and change robot settings (read back)."
