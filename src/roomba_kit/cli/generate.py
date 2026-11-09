"""Registry → Click commands (contracts/surfaces.md, CLI).

Each CLI operation becomes `roomba-kit <group…> <name> <args…> [--options]`. Service-backed
ops are sent through `ServiceClient` using the registry's shared params→HTTP mapping; `local`
ops (token admin) run in this process.
"""

import asyncio
import enum
import sys
import types
import typing
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from typing import Any

import click
from pydantic.fields import FieldInfo

from roomba_kit import __version__
from roomba_kit.cli.client import CliState, fail
from roomba_kit.cli.output import NO_TOKEN_HINT, emit_error, emit_response, emit_result, wire_json
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.tokens import TokenStore
from roomba_kit.core import registry
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.registry import Operation
from roomba_kit.service.describe import describe


@dataclass
class LocalContext:
    """The handler context for `local` ops: config files only, no robots, no service."""

    version: str = __version__

    @cached_property
    def tokens(self) -> TokenStore:
        return TokenStore(CredentialStore())


def group_for(root: click.Group, path: Iterable[str]) -> click.Group:
    """The group at `path` under `root`, created if missing. Extras use this to attach commands."""
    group = root
    for name in path:
        sub = group.commands.get(name)
        if sub is None:
            sub = click.Group(name)
            group.add_command(sub)
        if not isinstance(sub, click.Group):
            raise ValueError(f"{name!r} is a command, not a group")
        group = sub
    return group


def _unwrap(annotation: Any) -> tuple[Any, bool]:
    """(scalar type, is_list) for a field annotation, with `X | None` reduced to `X`."""
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        annotation = args[0] if len(args) == 1 else str
    if typing.get_origin(annotation) in (list, tuple, set, frozenset):
        inner = typing.get_args(annotation)
        return (inner[0] if inner else str), True
    return annotation, False


def _click_type(tp: Any) -> click.ParamType[Any]:
    if tp is int:
        return click.INT
    if tp is float:
        return click.FLOAT
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return click.Choice([str(m.value) for m in tp])
    if typing.get_origin(tp) is typing.Literal:
        return click.Choice([str(a) for a in typing.get_args(tp)])
    return click.STRING


def _param(name: str, field: FieldInfo, positional: bool) -> click.Parameter:
    tp, is_list = _unwrap(field.annotation)
    required = field.is_required()
    if positional:
        return click.Argument(
            [name], type=_click_type(tp), required=required, nargs=-1 if is_list else 1
        )
    flag = "--" + name.replace("_", "-")
    default = None if required else field.get_default(call_default_factory=True)
    shown = str(default) if default not in (None, [], ()) else False
    if tp is bool and not is_list:
        return click.Option(
            [f"{flag}/--no-{flag[2:]}", name],
            default=None,
            help=field.description,
            show_default=shown,
        )
    return click.Option(
        [flag, name],
        type=_click_type(tp),
        multiple=is_list,
        required=required,
        default=None,
        help=field.description,
        show_default=shown,
    )


def help_text(op: Operation) -> str:
    """`describe(op)`, with multi-line paragraphs marked so Click doesn't rewrap them."""
    paragraphs = describe(op).split("\n\n")
    return "\n\n".join("\b\n" + p if "\n" in p else p for p in paragraphs)


def read_secret(name: str) -> str:
    """Hidden prompt on a terminal; otherwise one line of stdin. Never a command-line option."""
    stdin = sys.stdin
    if stdin.isatty():
        return str(click.prompt(name.replace("_", " ").capitalize(), hide_input=True))
    line = stdin.readline().rstrip("\r\n")
    if not line:
        fail(ErrorCode.INVALID_REQUEST, f"No {name} given on stdin.")
    return line


def run(state: CliState, op: Operation, params: dict[str, Any]) -> None:
    essential = op.read_only or op.local
    if op.local:
        try:
            result = asyncio.run(registry.invoke(op, LocalContext(), params))
        except KitError as exc:
            emit_error(state, wire_json({"error": exc.to_dict()}), exc.to_dict())
        data = op.dump(result)
        emit_result(state, wire_json(data), data, essential=essential)
        return
    with state.client() as client:
        response = client.call(op, params)
    if response.status_code == 401 and not client.has_token:
        click.echo(NO_TOKEN_HINT, err=True)
    emit_response(state, response, essential=essential)


def command(op: Operation) -> click.Command:
    if (cli := op.cli) is None:
        raise ValueError(f"{op.name} has no CLI binding")
    fields = op.params.model_fields
    unknown = set(cli.args) | set(cli.secret)
    if missing := unknown - set(fields):
        raise ValueError(f"{op.name}: CLI params {sorted(missing)} not in params model")
    params = [_param(n, fields[n], positional=True) for n in cli.args]
    params += [
        _param(n, f, positional=False)
        for n, f in fields.items()
        if n not in cli.args and n not in cli.secret
    ]

    @click.pass_obj
    def callback(state: CliState, /, **values: Any) -> None:
        sent = {
            k: list(v) if isinstance(v, tuple) else v
            for k, v in values.items()
            if v is not None and v != ()
        }
        for name in cli.secret:
            sent[name] = read_secret(name)
        run(state, op, sent)

    return click.Command(cli.name, callback=callback, params=params, help=help_text(op))


def add_operations(root: click.Group, ops: Iterable[Operation]) -> None:
    for op in ops:
        if "cli" in op.surfaces and op.cli is not None:
            group_for(root, op.cli.group).add_command(command(op))
