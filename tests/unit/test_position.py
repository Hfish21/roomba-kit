import json

import httpx
import pytest
from click.testing import CliRunner
from fastmcp import Client
from fastmcp.exceptions import ToolError

from roomba_kit.cli import main, position_watch
from roomba_kit.cli.client import NOT_RUNNING, CliState
from roomba_kit.core.errors import CLI_EXIT, ErrorCode
from roomba_kit.core.models import ConnectionState
from roomba_kit.mcp.server import build_mcp, in_process
from roomba_kit.service import position
from roomba_kit.service.operations import load_operations
from tests.integration.test_us7_position import (  # noqa: F401 - fixtures
    I5,
    M6,
    ctx,
    factory,
    fix,
    poll_timeout,
    until,
)

FIX = fix(1.234).model_dump(mode="json")
NO_FIX = {"no_fix": True}


def test_defaults_match_research_r13():
    assert position.POLL_INTERVAL_S == 2.0
    assert position.POLL_TIMEOUT_S == 2.0
    assert position.GIVE_UP_AFTER == 3


def test_sse_parser_handles_comments_multiline_and_trailing_frames():
    lines = [
        ": keepalive",
        "id: 7",
        "event: position",
        "data: {",
        "data: }",
        "",
        "id: 8",
        "data: x",
    ]
    assert list(position_watch.sse_events(iter(lines))) == [
        ("7", "position", "{\n}"),
        ("8", None, "x"),
    ]


def test_format_fix():
    assert position_watch.format_fix(NO_FIX) == "no_fix"
    line = position_watch.format_fix(FIX)
    assert "x=1.23 m" in line and "y=-0.50 m" in line and "heading=3.10 rad" in line
    assert "map=map-1@v1" in line


def frame(seq: int, data: dict) -> str:
    envelope = {
        "schema": "roomba-kit.event/v1",
        "seq": seq,
        "type": "position",
        "robot_id": M6,
        "at": data.get("at"),
        "data": data,
    }
    return f"id: {seq}\nevent: position\ndata: {json.dumps(envelope)}\n\n"


class Service:
    """Answers get_position, then serves SSE streams in turn; once they run out, refuses."""

    def __init__(self, answer: dict, status: int = 200, streams: list[str] | None = None):
        self.answer, self.status, self.streams, self.requests = answer, status, streams or [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/position"):
            return httpx.Response(self.status, json=self.answer)
        if not self.streams:
            raise httpx.ConnectError("refused")
        body = self.streams.pop(0)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def invoke(args: list[str], service: Service):
    obj = CliState(transport=httpx.MockTransport(service))
    return CliRunner().invoke(main.build_cli(), args, obj=obj)


@pytest.fixture(autouse=True)
def fast_reconnect(monkeypatch):
    monkeypatch.setattr(position_watch, "RECONNECT_DELAY_S", 0)


def test_position_command_keeps_the_generated_shape(tmp_kit_home):
    service = Service(NO_FIX)
    result = invoke(["position", "Mop"], service)
    assert result.exit_code == 0, result.output
    assert result.stdout == "no_fix\n"
    (req,) = service.requests
    assert req.method == "GET" and req.url.path == "/v1/robots/Mop/position"


def test_json_output_is_the_body(tmp_kit_home):
    result = invoke(["--json", "position", M6], Service(FIX))
    assert json.loads(result.stdout_bytes) == FIX


def test_not_supported_exits_5(tmp_kit_home):
    error = {
        "error": {
            "code": "not_supported",
            "message": "Roomba i5/i5+ does not support live_position.",
            "details": {"profile": "roomba_i5", "capability": "live_position"},
        }
    }
    service = Service(error, status=422)
    result = invoke(["position", I5, "--watch"], service)
    assert result.exit_code == CLI_EXIT[ErrorCode.NOT_SUPPORTED]
    assert "not_supported" in result.stderr
    assert len(service.requests) == 1  # never opened the stream


def test_watch_shows_no_fix_then_streams_and_resumes(tmp_kit_home, monkeypatch):
    monkeypatch.setenv("ROOMBA_KIT_API_TOKEN", "rk_test")
    first = ": hello\n\n" + frame(41, FIX)
    second = frame(42, fix(2.0).model_dump(mode="json"))
    service = Service(NO_FIX, streams=[first, second])
    result = invoke(["position", "Mop", "--watch"], service)
    lines = result.stdout.splitlines()
    assert lines[0] == "no_fix"
    assert "x=1.23 m" in lines[1] and "x=2.00 m" in lines[2]
    # The service went away after the second stream: the start hint, exit 69.
    assert result.exit_code == 69 and NOT_RUNNING in result.stderr

    sse = [r for r in service.requests if r.url.path == "/v1/events"]
    assert len(sse) == 3
    assert sse[0].headers["accept"] == "text/event-stream"
    assert sse[0].headers["authorization"] == "Bearer rk_test"
    assert dict(sse[0].url.params) == {"types": "position", "robot": "Mop"}
    assert "last-event-id" not in sse[0].headers
    assert sse[1].headers["last-event-id"] == "41"
    assert sse[2].headers["last-event-id"] == "42"


def test_watch_json_prints_one_envelope_per_line(tmp_kit_home):
    service = Service(FIX, streams=[frame(5, FIX)])
    result = invoke(["--json", "position", M6, "--watch"], service)
    first, second = result.stdout.splitlines()
    assert json.loads(first) == FIX
    assert json.loads(second)["seq"] == 5 and json.loads(second)["data"] == FIX


async def test_mcp_get_position(ctx, factory):  # noqa: F811
    load_operations()
    await ctx.start()
    try:
        await until(lambda: all(s.connection is ConnectionState.CONNECTED for s in ctx))
        async with Client(build_mcp(in_process(ctx))) as client:
            result = await client.call_tool("get_position", {"robot": M6})
            assert result.structured_content in (NO_FIX, {"result": NO_FIX})
            with pytest.raises(ToolError, match="^not_supported"):
                await client.call_tool("get_position", {"robot": I5})
        assert factory.get(I5).position_requests == 0
    finally:
        await ctx.stop()
