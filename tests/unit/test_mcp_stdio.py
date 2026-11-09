import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from click.testing import CliRunner
from fastmcp import Client
from fastmcp.exceptions import ToolError

from roomba_kit.cli import mcp_stdio
from roomba_kit.cli.main import build_cli
from roomba_kit.core import registry
from roomba_kit.mcp.server import build_mcp, in_process
from roomba_kit.mcp.stdio import NO_TOKEN, NOT_RUNNING, ServiceExecutor, build_stdio_mcp
from roomba_kit.service.operations import load_operations
from roomba_kit.transport.fake import FakeTransportFactory
from tests.integration._us4 import FIXTURES, make_ctx, sample_params, serving
from tests.unit.test_api import I5, register_test_operations


class Service:
    """An async MockTransport handler standing in for the service; records requests."""

    def __init__(self, status: int = 200, body: Any = None, exc: Exception | None = None):
        self.status, self.body, self.exc = status, body, exc
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.exc is not None:
            raise self.exc
        content = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return httpx.Response(self.status, content=content)


@pytest.fixture
def test_ops() -> Iterator[None]:
    load_operations()
    with registry.isolated():
        register_test_operations()
        yield


def proxy(service: Service, token: str | None = "rk_test") -> Client[Any]:
    executor = ServiceExecutor(
        "http://svc:1234/", token=lambda: token, transport=httpx.MockTransport(service)
    )
    return Client(build_mcp(executor))


async def test_calls_go_to_the_http_route_with_the_token(test_ops):
    service = Service(body={"robot": I5, "fields": ["a", "b"], "limit": 10})
    async with proxy(service) as client:
        result = await client.call_tool("robot_state", {"robot": I5, "fields": ["a", "b"]})
    assert result.structured_content == {"robot": I5, "fields": ["a", "b"], "limit": 10}
    (req,) = service.requests
    assert req.method == "GET"
    assert str(req.url) == f"http://svc:1234/v1/robots/{I5}/state?fields=a&fields=b"
    assert req.headers["authorization"] == "Bearer rk_test"


async def test_post_body_and_unconfirmed_result(test_ops):
    body = {
        "robot_id": I5,
        "command": "dock",
        "outcome": "unconfirmed",
        "state": {"activity": "cleaning"},
        "waited_ms": 5,
    }
    service = Service(status=202, body=body)
    async with proxy(service) as client:
        result = await client.call_tool("dock", {"robot": I5, "wait": False})
    assert result.is_error is False and result.structured_content["outcome"] == "unconfirmed"
    assert json.loads(service.requests[0].content) == {"wait": False}


async def test_error_envelope_becomes_the_same_tool_error(test_ops):
    error = {
        "code": "not_supported",
        "message": "Braava jet m6 does not support evac.",
        "robot_id": I5,
        "details": {"capability": "evac"},
    }
    async with proxy(Service(status=422, body={"error": error})) as client:
        with pytest.raises(ToolError) as info:
            await client.call_tool("evac", {"robot": I5})
    text = str(info.value)
    assert text.startswith("not_supported: Braava jet m6 does not support evac.")
    assert json.loads(text.split("\n", 1)[1]) == {"error": error}


async def test_unauthorized_without_a_token_hints(test_ops):
    body = {"error": {"code": "unauthorized", "message": "Missing or invalid API token."}}
    service = Service(status=401, body=body)
    async with proxy(service, token=None) as client:
        with pytest.raises(ToolError, match=f"^unauthorized: .*{NO_TOKEN}"):
            await client.call_tool("robot_state", {"robot": I5})
    assert "authorization" not in service.requests[0].headers


async def test_invalid_arguments_are_rejected_before_sending(test_ops):
    service = Service(body={})
    async with proxy(service) as client:
        with pytest.raises(ToolError, match="^invalid_request: "):
            await client.call_tool("robot_state", {"limit": "lots"})
    assert service.requests == []


async def test_unexpected_response_is_internal(test_ops):
    async with proxy(Service(status=502, body=b"<html>bad gateway</html>")) as client:
        with pytest.raises(ToolError, match="^internal: .*HTTP 502"):
            await client.call_tool("robot_state", {"robot": I5})


@pytest.mark.parametrize(
    "exc", [httpx.ConnectError("refused"), httpx.ReadError("reset")], ids=["down", "dropped"]
)
async def test_every_tool_is_service_unavailable_when_the_service_is_down(exc, tmp_kit_home):
    load_operations()
    ops = registry.on("mcp")
    assert ops
    async with proxy(Service(exc=exc)) as client:
        for op in ops:
            result = await client.call_tool(op.name, sample_params(op), raise_on_error=False)
            assert result.is_error, op.name
            assert result.content[0].text.startswith("service_unavailable: "), op.name
    if isinstance(exc, httpx.ConnectError):
        assert NOT_RUNNING in result.content[0].text


async def test_tools_are_the_in_service_tools():
    load_operations()

    def listed(tools: list[Any]) -> list[dict[str, Any]]:
        return [t.model_dump(exclude={"meta"}) for t in tools]

    async with Client(build_mcp(in_process(object()))) as in_service:  # type: ignore[arg-type]
        expected = listed(await in_service.list_tools())
    async with Client(build_stdio_mcp("http://127.0.0.1:9")) as stdio:
        assert listed(await stdio.list_tools()) == expected
    assert {t["name"] for t in expected} == {op.name for op in registry.on("mcp")}


def test_mcp_command_serves_stdio_at_the_service_url(tmp_kit_home, monkeypatch):
    seen: list[str] = []

    async def fake_serve(url: str) -> None:
        seen.append(url)

    monkeypatch.setattr(mcp_stdio, "serve_stdio", fake_serve)
    result = CliRunner().invoke(build_cli(), ["--service-url", "http://192.0.2.5:8765", "mcp"])
    assert result.exit_code == 0, result.output
    assert seen == ["http://192.0.2.5:8765"]
    assert result.stdout == ""


async def test_proxy_against_the_real_api(tmp_kit_home):
    factory = FakeTransportFactory.from_fixtures(FIXTURES, reaction_delay=0.01)
    async with serving(make_ctx(factory)) as served:
        executor = ServiceExecutor("http://test", transport=served.transport)
        async with Client(build_mcp(executor)) as client:
            state = await client.call_tool("get_state", {"robot": "Vacuum"})
            expected = await served.client.get("/v1/robots/Vacuum/state", headers=served.auth)
            assert state.structured_content == expected.json()
            with pytest.raises(ToolError, match="^robot_not_found: "):
                await client.call_tool("mission_wait", {"robot": "Nobody", "timeout_s": 0.05})
