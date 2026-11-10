"""`roomba-kit position ROBOT [--watch]`: replaces the generated `position` command.

Without `--watch` it's `get_position`. With it, it prints the current answer (so a docked robot
shows `no_fix`), then follows the service's live `position` events over SSE
(`GET /v1/events`, `Accept: text/event-stream`). Presentation only.
"""

import json
import sys
import time
from collections.abc import Iterator
from typing import Any

import click
import httpx

from roomba_kit.cli.client import NOT_RUNNING, CliState, cli_token, fail
from roomba_kit.cli.generate import help_text
from roomba_kit.cli.output import NO_TOKEN_HINT, emit_response, render
from roomba_kit.core import registry
from roomba_kit.core.contract import API_PREFIX
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.registry import Operation

OP_NAME = "get_position"
RECONNECT_DELAY_S = 1.0
"""Pause before resuming a stream the service closed (shutdown or a lagging consumer)."""


def format_fix(value: Any) -> str:
    """One line per answer: a fix, or `no_fix`."""
    if not isinstance(value, dict):
        return render(value)
    if value.get("no_fix"):
        return "no_fix"
    at = str(value.get("at", ""))
    clock = at[11:19] if len(at) >= 19 else at
    map_ref = value.get("map_id") or "-"
    if value.get("map_version"):
        map_ref += f"@{value['map_version']}"
    return (
        f"{clock}  x={value['x_m']:.2f} m  y={value['y_m']:.2f} m  "
        f"heading={value['heading_rad']:.2f} rad  map={map_ref}"
    )


def sse_events(lines: Iterator[str]) -> Iterator[tuple[str | None, str | None, str]]:
    """`(id, event, data)` for each SSE frame; comments and frames without data are skipped."""
    seq: str | None = None
    kind: str | None = None
    data: list[str] = []
    for line in lines:
        if not line:
            if data:
                yield seq, kind, "\n".join(data)
            kind, data = None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "id":
            seq = value
        elif field == "event":
            kind = value
        elif field == "data":
            data.append(value)
    if data:
        yield seq, kind, "\n".join(data)


def _show(state: CliState, response: httpx.Response, has_token: bool, *, lines: bool) -> None:
    """Print one answer. With `lines`, `--json` output ends in a newline (NDJSON for --watch)."""
    if response.status_code == 401 and not has_token:
        click.echo(NO_TOKEN_HINT, err=True)
    if not response.is_success:
        emit_response(state, response)
    elif state.json:
        click.echo(response.content, nl=lines or sys.stdout.isatty())
    else:
        click.echo(format_fix(response.json()))


def _stream(state: CliState, robot: str, last_id: str | None) -> str | None:
    """Follow one SSE connection until the service closes it; returns the last event ID seen."""
    token = cli_token()
    headers = {"Accept": "text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if last_id is not None:
        headers["Last-Event-ID"] = last_id
    params = {"types": "position", "robot": robot}
    base = state.url().rstrip("/") + API_PREFIX
    timeout = httpx.Timeout(5.0, read=None)
    try:
        with (
            httpx.Client(base_url=base, transport=state.transport, timeout=timeout) as http,
            http.stream("GET", "/events", params=params, headers=headers) as response,
        ):
            if not response.headers.get("content-type", "").startswith("text/event-stream"):
                response.read()
                _show(state, response, token is not None, lines=True)
                fail(ErrorCode.INTERNAL, "The service didn't open an event stream.")
            for seq, _, data in sse_events(response.iter_lines()):
                last_id = seq or last_id
                envelope = json.loads(data)
                if state.json:
                    click.echo(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
                else:
                    click.echo(format_fix(envelope.get("data")))
    except (httpx.ConnectError, httpx.ConnectTimeout):
        fail(ErrorCode.SERVICE_UNAVAILABLE, NOT_RUNNING)
    except httpx.TransportError as exc:
        fail(ErrorCode.SERVICE_UNAVAILABLE, f"Lost the connection to the service: {exc}")
    return last_id


def watch(state: CliState, robot: str) -> None:
    last_id: str | None = None
    try:
        while True:
            last_id = _stream(state, robot, last_id)
            time.sleep(RECONNECT_DELAY_S)
    except KeyboardInterrupt:
        return


def position_command(op: Operation) -> click.Command:
    @click.command(
        "position",
        help=help_text(op)
        + "\n\nWith --watch, print the current answer, then every fix as it arrives "
        "(Ctrl-C to stop). Fixes arrive while the robot is cleaning, mapping or returning.",
    )
    @click.argument("robot")
    @click.option("--watch", "watch_", is_flag=True, help="Follow live position fixes.")
    @click.pass_obj
    def position(state: CliState, robot: str, watch_: bool) -> None:
        with state.client() as client:
            response = client.call(op, {"robot": robot})
        _show(state, response, client.has_token, lines=watch_)
        if watch_:
            watch(state, robot)

    return position


def register(root: click.Group) -> None:
    if "position" not in root.commands:  # the registry in use has no get_position
        return
    try:
        op = registry.get(OP_NAME)
    except KitError:
        return
    root.add_command(position_command(op))
