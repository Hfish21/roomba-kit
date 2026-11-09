"""SSE at `GET /v1/events` (T053) and `events watch` (T054). httpx's ASGI transport buffers
whole responses, so the server tests drive the ASGI app directly and read frames as sent."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from click.testing import CliRunner
from fastapi import FastAPI

from roomba_kit.api.app import create_app
from roomba_kit.cli import events_watch
from roomba_kit.cli.client import CliState
from roomba_kit.cli.main import build_cli
from roomba_kit.core.events import EventType
from roomba_kit.service.app import ServiceContext
from tests.integration.test_us2_state_events import I5, M6, ctx, factory, until  # noqa: F401


@asynccontextmanager
async def running(context: ServiceContext) -> AsyncIterator[FastAPI]:
    app = create_app(context)
    started, stop = asyncio.Event(), asyncio.Event()

    async def lifespan() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(lifespan())
    await until(lambda: started.is_set())
    await until(lambda: all(s.has_state for s in context))
    try:
        yield app
    finally:
        stop.set()
        await task


class Stream:
    """One open `GET /v1/events` request, read frame by frame."""

    def __init__(self, app: FastAPI, token: str, query: str = "", **headers: str) -> None:
        raw = [(b"authorization", f"Bearer {token}".encode())]
        raw += [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
        self.scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/events",
            "raw_path": b"/v1/events",
            "root_path": "",
            "query_string": query.encode(),
            "headers": raw,
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }
        self.app, self.status = app, 0
        self.headers: dict[str, str] = {}
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        self._buffer = ""
        self._requested = False
        self._gone = asyncio.Event()
        self._started = asyncio.Event()

    async def _receive(self) -> dict[str, Any]:
        if not self._requested:
            self._requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode(): v.decode() for k, v in message["headers"]}
            self._started.set()
        elif message.get("body"):
            self._chunks.put_nowait(message["body"])

    async def __aenter__(self) -> "Stream":
        self._task = asyncio.create_task(self.app(self.scope, self._receive, self._send))
        await asyncio.wait_for(self._started.wait(), 3)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._gone.set()
        await asyncio.wait_for(self._task, 3)

    async def body(self) -> bytes:
        await asyncio.wait_for(self._task, 3)
        out = b""
        while not self._chunks.empty():
            out += self._chunks.get_nowait()
        return out

    async def frame(self, timeout: float = 3.0) -> dict[str, str]:
        """The next event frame; ping comments are skipped."""
        async with asyncio.timeout(timeout):
            while True:
                text = self._buffer.replace("\r\n", "\n")
                if "\n\n" in text:
                    block, self._buffer = text.split("\n\n", 1)
                    fields = dict(
                        line.split(": ", 1) for line in block.splitlines() if ": " in line
                    )
                    if "data" in fields:
                        return fields
                    continue
                self._buffer = text + (await self._chunks.get()).decode()


async def test_streams_replay_then_live_with_seq_ids(ctx, factory):  # noqa: F811
    async with running(ctx) as app:
        token = ctx.tokens.create("sse")
        head = ctx.bus.last_seq
        events, _ = await ctx.bus.read(0, limit=1000)
        query = f"since={events[0].seq - 1}"
        async with Stream(app, token, query, accept="text/event-stream") as s:
            assert s.status == 200 and s.headers["content-type"].startswith("text/event-stream")
            first = await s.frame()
            assert first["id"] == str(events[0].seq) and first["event"] == events[0].type.value
            envelope = json.loads(first["data"])
            assert envelope["schema"] == "roomba-kit.event/v1" and envelope["seq"] == events[0].seq
            seen = [int(first["id"])]
            while seen[-1] < head:
                seen.append(int((await s.frame())["id"]))
            assert seen == sorted(set(seen))

            # SC-005: a robot report reaches the subscriber well within 2 s.
            status = factory.get(I5).reported["cleanMissionStatus"]
            sent = time.monotonic()
            factory.get(I5).emit({"cleanMissionStatus": {**status, "error": 18}})
            while (live := await s.frame())["event"] != "error.raised":
                pass
            assert time.monotonic() - sent < 2.0
            assert int(live["id"]) > head and json.loads(live["data"])["data"]["code"] == 18


async def test_last_event_id_resumes_without_gap_or_repeat(ctx, factory):  # noqa: F811
    async with running(ctx) as app:
        token = ctx.tokens.create("sse")
        status = factory.get(M6).reported["cleanMissionStatus"]
        for code in (18, 0, 19):
            factory.get(M6).emit({"cleanMissionStatus": {**status, "error": code}})
        types = [EventType.ERROR_RAISED, EventType.ERROR_CLEARED]
        await until(
            lambda: (
                ctx.sessions[M6].state.error is not None and ctx.sessions[M6].state.error.code == 19
            )
        )
        errors, _ = await ctx.bus.read(0, M6, types)
        assert len(errors) == 3
        query = f"robot=Mop&types=error.raised&types=error.cleared&since={errors[-1].seq}"
        headers = {"accept": "text/event-stream", "last_event_id": str(errors[0].seq)}
        async with Stream(app, token, query, **headers) as s:  # Last-Event-ID wins over since
            got = [await s.frame(), await s.frame()]
        assert [int(f["id"]) for f in got] == [e.seq for e in errors[1:]]
        assert [f["event"] for f in got] == ["error.cleared", "error.raised"]


async def test_json_page_without_the_accept_header_and_bad_ids(ctx):  # noqa: F811
    async with running(ctx) as app:
        token = ctx.tokens.create("sse")
        async with Stream(app, token, "limit=2") as s:
            page = json.loads(await s.body())
        assert s.status == 200 and len(page["events"]) == 2 and page["next"] > 0
        bad = Stream(app, token, accept="text/event-stream", last_event_id="soon")
        async with bad as s:
            body = json.loads(await s.body())
        assert s.status == 400 and body["error"]["code"] == "invalid_request"


async def test_unknown_robot_is_an_error_not_an_empty_stream(ctx):  # noqa: F811
    async with running(ctx) as app:
        token = ctx.tokens.create("sse")
        async with Stream(app, token, "robot=Nobody", accept="text/event-stream") as s:
            body = json.loads(await s.body())
        assert s.status == 404 and body["error"]["code"] == "robot_not_found"


# -- `roomba-kit events watch` (T054) ------------------------------------------------------------


def _envelope(seq: int, type_: str, data: dict[str, Any]) -> dict[str, Any]:
    at = "2026-11-08T20:44:12Z"
    return {"schema": "roomba-kit.event/v1", "seq": seq, "type": type_, "robot_id": I5, "at": at,
            "data": data}  # fmt: skip


def _sse(*envelopes: dict[str, Any]) -> bytes:
    """Frames exactly as sse-starlette writes them (CRLF), after a ping comment."""
    out = ": ping - 2026-11-08 20:44:12\r\n\r\n"
    for e in envelopes:
        data = json.dumps(e, separators=(",", ":"))
        out += f"id: {e['seq']}\r\nevent: {e['type']}\r\ndata: {data}\r\n\r\n"
    return out.encode()


class Service:
    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses, self.requests = list(responses), []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _watch(service: Service, *args: str) -> Any:
    obj = CliState(transport=httpx.MockTransport(service), service_url="http://test")
    return CliRunner().invoke(build_cli(), [*args], obj=obj)


DOCKED = _envelope(7, "robot.docked", {"mission_id": "M1"})
FINISHED = _envelope(8, "mission.finished", {"mission": {"id": "M1", "outcome": "completed"}})
SETTING = _envelope(9, "setting.changed", {"setting": "child_lock", "from": False, "to": True})
STOP = httpx.Response(401, json={"error": {"code": "unauthorized", "message": "Bad token."}})


def test_watch_prints_lines_and_resumes_with_last_event_id(tmp_kit_home, monkeypatch):
    monkeypatch.setattr(events_watch, "RECONNECT_DELAYS", (0,))
    service = Service(
        httpx.Response(200, content=_sse(DOCKED, FINISHED)),
        httpx.ReadError("dropped"),
        httpx.Response(200, content=_sse(SETTING)),
        STOP,
    )
    result = _watch(service, "events", "watch", "--robot", "Vacuum", "--type", "mission.finished")
    assert result.exit_code == 77, result.output
    lines = result.stdout.splitlines()
    assert "robot.docked" in lines[0] and "mission_id=M1" in lines[0]
    assert "mission=M1 outcome=completed rooms=whole home" in lines[1]
    assert "child_lock: False -> True" in lines[2]
    assert [r.headers.get("last-event-id") for r in service.requests] == [None, "8", "8", "9"]
    assert all(r.headers["accept"] == "text/event-stream" for r in service.requests)
    assert service.requests[0].url.params.get_list("types") == ["mission.finished"]
    assert service.requests[0].url.params["robot"] == "Vacuum"


def test_watch_json_lines_are_the_envelopes(tmp_kit_home, monkeypatch):
    monkeypatch.setattr(events_watch, "RECONNECT_DELAYS", (0,))
    service = Service(httpx.Response(200, content=_sse(DOCKED, FINISHED)), STOP)
    result = _watch(service, "--json", "events", "watch", "--since", "3")
    assert [json.loads(x) for x in result.stdout.splitlines()[:2]] == [DOCKED, FINISHED]
    assert service.requests[0].headers["last-event-id"] == "3"


def test_watch_without_a_service_exits_69(tmp_kit_home):
    result = _watch(Service(httpx.ConnectError("refused")), "events", "watch")
    assert result.exit_code == 69 and "Service not running" in result.stderr
