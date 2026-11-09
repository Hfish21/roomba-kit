"""Every CLI command's help is a contract for scripts and people: changes show as diffs."""

import click

from roomba_kit.cli.main import build_cli
from tests.contract._snapshot import check

WIDTH = 100


def _help_pages(root: click.Group) -> str:
    top = click.Context(
        root,
        info_name="roomba-kit",
        terminal_width=WIDTH,
        max_content_width=WIDTH,
        help_option_names=root.context_settings.get("help_option_names"),
    )
    pages: list[str] = []

    def walk(ctx: click.Context) -> None:
        pages.append(f"$ {ctx.command_path} --help\n{ctx.get_help()}\n")
        if isinstance(ctx.command, click.Group):
            for name in sorted(ctx.command.commands):
                walk(click.Context(ctx.command.commands[name], info_name=name, parent=ctx))

    walk(top)
    return "\n".join(pages)


def test_cli_help_snapshot(tmp_kit_home):
    text = _help_pages(build_cli())
    assert str(tmp_kit_home) not in text, "help must not depend on this machine's paths"
    check("cli_help.txt", text)
