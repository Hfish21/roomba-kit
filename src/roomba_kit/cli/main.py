"""The `roomba-kit` command: global options, registry commands, then every module's extras.

Extension seam: any `roomba_kit.cli.<module>` defining `register(root: click.Group)` is imported
and called after the generated commands are added, in module-name order. `register` may add
commands anywhere (`generate.group_for(root, path)`) and replaces a generated command by adding
one under the same name: Click groups key commands by name.
"""

import importlib
import pkgutil
from collections.abc import Iterable

import click

import roomba_kit.cli
from roomba_kit.cli import generate
from roomba_kit.cli.client import SERVICE_URL_ENV, CliState
from roomba_kit.core import registry
from roomba_kit.core.registry import Operation
from roomba_kit.service.operations import load_operations

CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def _root() -> click.Group:
    @click.group(context_settings=CONTEXT_SETTINGS)
    @click.option("--json", "json_", is_flag=True, help="Machine output: the HTTP JSON body.")
    @click.option(
        "--service-url",
        envvar=SERVICE_URL_ENV,
        metavar="URL",
        help="The service to talk to. Default: http://127.0.0.1:<server.port> from config.toml "
        f"(http://127.0.0.1:8765). Env: {SERVICE_URL_ENV}.",
    )
    @click.option("--quiet", "-q", is_flag=True, help="Suppress non-essential output.")
    @click.pass_context
    def root(ctx: click.Context, json_: bool, service_url: str | None, quiet: bool) -> None:
        """Roomba Kit: local-first control for iRobot robots.

        Most commands talk to the background service (`roomba-kit service start`). Lifecycle
        and token commands work without it.
        """
        state = ctx.ensure_object(CliState)
        state.json, state.quiet, state.service_url = json_, quiet, service_url

    root.name = "roomba-kit"
    return root


def extension_modules() -> list[str]:
    return sorted(
        f"roomba_kit.cli.{m.name}"
        for m in pkgutil.iter_modules(roomba_kit.cli.__path__)
        if m.name != "main"
    )


def build_cli(ops: Iterable[Operation] | None = None) -> click.Group:
    """A fresh root: generated commands for `ops` (default: the real registry), then extras."""
    if ops is None:
        load_operations()
        ops = registry.on("cli")
    root = _root()
    generate.add_operations(root, ops)
    for name in extension_modules():
        register = getattr(importlib.import_module(name), "register", None)
        if callable(register):
            register(root)
    return root


cli = build_cli()

if __name__ == "__main__":
    cli()
