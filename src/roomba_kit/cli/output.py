"""Rendering results and errors, and the exit-code mapping (contracts/errors.md, surfaces.md).

`--json` writes the service's response body unchanged, so CLI output is byte-identical to the
HTTP API. Human output is a table for lists, `key: value` lines for objects.
"""

import json
import sys
from typing import Any, NoReturn

import click
import httpx

from roomba_kit.cli.client import CliState, fail
from roomba_kit.core.errors import CLI_EXIT, ErrorCode

UNCONFIRMED_EXIT = CLI_EXIT[ErrorCode.UNCONFIRMED]
NO_TOKEN_HINT = "No API token found. Create one with: roomba-kit token create cli"  # noqa: S105


def wire_json(value: Any) -> bytes:
    """JSON encoded the way the API's responses are (compact, UTF-8), for local results."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _table_cell(value: Any) -> str:
    """Like `_cell`, but nested lists of objects are summarised so table rows stay readable
    (`roomba-kit robots list` otherwise prints every capability as raw JSON). `--json` and
    `robots show` keep the full detail."""
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
        verified = [v for v in value if isinstance(v.get("verification"), dict)]
        if len(verified) == len(value):
            ok = sum(v["verification"].get("status") == "verified" for v in verified)
            return f"{len(value)} ({ok} verified)"
        return f"{len(value)} items"
    return _cell(value)


def render(value: Any) -> str:
    """Human text for a decoded JSON result."""
    if isinstance(value, list):
        if not value:
            return "(none)"
        if all(isinstance(row, dict) for row in value):
            cols = list(dict.fromkeys(k for row in value for k in row))
            rows = [[_table_cell(row.get(c)) for c in cols] for row in value]
            widths = [max(len(c), *(len(r[i]) for r in rows)) for i, c in enumerate(cols)]
            lines = [cols, *rows]
            return "\n".join(
                "  ".join(cell.ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip()
                for line in lines
            )
        return "\n".join(_cell(v) for v in value)
    if isinstance(value, dict):
        width = max((len(k) for k in value), default=0)
        return "\n".join(f"{k.ljust(width)}  {_cell(v)}" for k, v in value.items())
    return _cell(value)


def emit_result(state: CliState, body: bytes, value: Any, *, essential: bool = True) -> None:
    """Print a successful result; exit 3 when it's a CommandResult that wasn't confirmed."""
    if state.json:
        click.echo(body, nl=sys.stdout.isatty())
    elif essential or not state.quiet:
        click.echo(render(value))
    if isinstance(value, dict) and value.get("outcome") == "unconfirmed":
        raise SystemExit(UNCONFIRMED_EXIT)


def emit_error(state: CliState, body: bytes, error: dict[str, Any]) -> NoReturn:
    """Print an error envelope's message on stderr (or the body with `--json`) and exit."""
    try:
        code = ErrorCode(str(error.get("code")))
    except ValueError:
        code = ErrorCode.INTERNAL
    if state.json:
        click.echo(body, nl=sys.stdout.isatty())
        raise SystemExit(CLI_EXIT[code])
    message = f"Error ({code.value}): {error.get('message', '')}"
    if details := error.get("details"):
        message += "\n" + render(details)
    fail(code, message)


def emit_response(state: CliState, response: httpx.Response, *, essential: bool = True) -> None:
    body = response.content
    try:
        value = response.json()
    except ValueError:
        value = None
    if isinstance(value, dict) and isinstance(value.get("error"), dict):
        emit_error(state, body, value["error"])
    if response.is_error or (value is None and body):
        fail(
            ErrorCode.INTERNAL, f"Unexpected response from the service: HTTP {response.status_code}"
        )
    emit_result(state, body, value, essential=essential)
