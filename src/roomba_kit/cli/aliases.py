"""`roomba-kit clean ROBOT [--room NAME_OR_ID …] [--unordered]`: replaces the generated `clean`.

With no `--room` it's the `clean` operation; with rooms it's `clean_rooms` (contracts/surfaces.md:
the one CLI name that differs from its operation). The generated `clean_rooms` command stays at
`roomba-kit rooms clean`. Presentation only: both paths go through `generate.run`, so exit codes
and `--json` output are those of the generated commands.
"""

import click

from roomba_kit.cli.client import CliState, fail
from roomba_kit.cli.generate import help_text, run
from roomba_kit.core import registry
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.registry import Operation


def _op(name: str) -> Operation | None:
    try:
        return registry.get(name)
    except KitError:
        return None


def clean_command(clean: Operation | None, clean_rooms: Operation) -> click.Command:
    summary = help_text(clean) if clean is not None else "Clean the given rooms."

    @click.command(
        "clean",
        help=summary + "\n\nWith --room, clean only those rooms, in the order given "
        "(`clean_rooms`; see `roomba-kit rooms clean --help`).",
    )
    @click.argument("robot")
    @click.option(
        "--room",
        "rooms",
        multiple=True,
        metavar="NAME_OR_ID",
        help="A room to clean, by name or ID. Repeat for several, in cleaning order.",
    )
    @click.option("--unordered", is_flag=True, help="Let the robot choose the room order.")
    @click.pass_obj
    def command(state: CliState, robot: str, rooms: tuple[str, ...], unordered: bool) -> None:
        if rooms:
            run(
                state, clean_rooms, {"robot": robot, "rooms": list(rooms), "ordered": not unordered}
            )
        elif unordered:
            fail(ErrorCode.INVALID_REQUEST, "--unordered needs at least one --room.")
        elif clean is None:
            fail(ErrorCode.INVALID_REQUEST, "Whole-home cleaning isn't available; use --room.")
        else:
            run(state, clean, {"robot": robot})

    return command


def register(root: click.Group) -> None:
    clean_rooms = _op("clean_rooms")
    if clean_rooms is None or clean_rooms.cli is None:  # the registry in use has no room cleans
        return
    clean = _op("clean") if "clean" in root.commands else None
    root.add_command(clean_command(clean, clean_rooms))
