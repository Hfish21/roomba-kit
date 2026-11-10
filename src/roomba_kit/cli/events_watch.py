"""`roomba-kit events watch`: follow the service's SSE event stream (T054).

One line per event, or the event envelope as one JSON line with `--json`. When the stream drops
(service restart, network blip) it reconnects with `Last-Event-ID`, so no event is missed or
repeated. Runs until interrupted.
"""

import json
import time
from collections.abc import Iterator
from typing import Any

import click
import httpx

from roomba_kit.cli.client import NOT_RUNNING, CliState, cli_token, fail
from roomba_kit.cli.generate import group_for
from roomba_kit.cli.output import NO_TOKEN_HINT, emit_response
from roomba_kit.core.contract import API_PREFIX
from roomba_kit.core.errors import ErrorCode
from roomba_kit.core.events import EventType

RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0)
"""Seconds to wait before each reconnect attempt; the last repeats. Tests shorten them."""


def frames(lines: Iterator[str]) -> Iterator[dict[str, str]]:
    """SSE frames as `{id, event, data}` dicts. Comment lines (pings) are skipped."""
    fields: dict[str, str] = {}
    for line in lines:
        if not line:
            if "data" in fields:
                yield fields
            fields = {}
        elif not line.startswith(":"):
            name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            fields[name] = (
                f"{fields[name]}\n{value}" if name == "data" and name in fields else value
            )


def summary(event: dict[str, Any]) -> str:
    data: dict[str, Any] = event.get("data") or {}
    if "mission" in data:
        mission = data["mission"] or {}
        rooms = ",".join(mission.get("rooms_requested") or []) or "whole home"
        return f"mission={mission.get('id')} outcome={mission.get('outcome')} rooms={rooms}"
    if "state" in data:
        return "changed=" + ",".join(data.get("changed") or [])
    if "from" in data and "to" in data:
        head = f"{data['setting']}: " if "setting" in data else ""
        rest = {k: v for k, v in data.items() if k not in ("from", "to", "setting")}
        return head + f"{data['from']} -> {data['to']}" + "".join(_pairs(rest))
    return "".join(_pairs(data)).strip()


def _pairs(data: dict[str, Any], lead: str = " ") -> Iterator[str]:
    for key, value in data.items():
        if value is not None:
            shown = (
                json.dumps(value, separators=(",", ":"))
                if isinstance(value, dict | list)
                else value
            )
            yield f"{lead}{key}={shown}"


def line(event: dict[str, Any]) -> str:
    robot = (event.get("robot_id") or "-")[-6:]
    head = f"{event.get('at')}  {event.get('seq'):>6}  {event.get('type', ''):<26} {robot}"
    return f"{head}  {summary(event)}".rstrip()


def _client(state: CliState) -> httpx.Client:
    token = cli_token()
    headers = {"Accept": "text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # No read timeout: the service pings every 15 s, and a dead link ends the stream instead.
    return httpx.Client(
        base_url=state.url().rstrip("/") + API_PREFIX,
        headers=headers,
        transport=state.transport,
        timeout=httpx.Timeout(5.0, read=None),
    )


def watch(state: CliState, robot: str | None, types: list[str], since: int | None) -> None:
    last, attempt, ever_connected = since, 0, False
    with _client(state) as client:
        while True:
            params: dict[str, Any] = {"robot": robot, "types": types or None}
            headers = {"Last-Event-ID": str(last)} if last is not None else {}
            try:
                with client.stream(
                    "GET", "/events", params={k: v for k, v in params.items() if v}, headers=headers
                ) as response:
                    if response.status_code != 200:
                        response.read()
                        if response.status_code == 401 and not cli_token():
                            click.echo(NO_TOKEN_HINT, err=True)
                        emit_response(state, response)
                        return
                    ever_connected, attempt = True, 0
                    for frame in frames(response.iter_lines()):
                        last = int(frame["id"]) if frame.get("id", "").isdigit() else last
                        if state.json:
                            click.echo(frame["data"])
                        else:
                            click.echo(line(json.loads(frame["data"])))
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if not ever_connected:
                    fail(ErrorCode.SERVICE_UNAVAILABLE, NOT_RUNNING)
            except httpx.TransportError:
                pass
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            attempt += 1
            if not state.quiet:
                where = f" after event {last}" if last is not None else ""
                click.echo(f"Stream ended; reconnecting{where} in {delay:g}s…", err=True)
            time.sleep(delay)


def register(root: click.Group) -> None:
    @click.command("watch")
    @click.option("--robot", help="Only this robot (ID or unambiguous name)")
    @click.option(
        "--type",
        "types",
        multiple=True,
        type=click.Choice([t.value for t in EventType]),
        help="Only these event types (repeatable)",
    )
    @click.option("--since", type=int, help="Replay events after this seq first")
    @click.pass_obj
    def watch_cmd(
        state: CliState, robot: str | None, types: tuple[str, ...], since: int | None
    ) -> None:
        """Follow events live, one line each (`--json`: one envelope per line). Ctrl-C to stop."""
        try:
            watch(state, robot, list(types), since)
        except KeyboardInterrupt:
            return

    group_for(root, ("events",)).add_command(watch_cmd)
