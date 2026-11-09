import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from roomba_kit.api import generate
from roomba_kit.api.app import create_app, register_stream_presenter, stream_presenters
from roomba_kit.config.settings import load_settings
from roomba_kit.core import registry
from roomba_kit.core.capabilities import Capability
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import Activity, CommandResult, StateSummary
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.operations import load_operations
from roomba_kit.transport.fake import FakeTransportFactory

I5 = "00000000000000000000000000000001"
LEAKY = "rk_this-must-never-leak"


class StateParams(BaseModel):
    robot: str = Field(description="Robot ID or name")
    fields: list[str] | None = Field(default=None, description="Fields to include")
    limit: int = 10


class Echo(BaseModel):
    robot: str
    fields: list[str] | None
    limit: int


class DockParams(BaseModel):
    robot: str
    wait: bool = True
    note: str | None = None


class Room(BaseModel):
    id: str
    name: str


class Health(BaseModel):
    status: str


class Token(BaseModel):
    name: str


def register_test_operations() -> None:
    """Small operations covering every surface rule. Call inside `registry.isolated()`."""

    @operation(
        description="Read a robot's state.",
        params=StateParams,
        result=Echo,
        read_only=True,
        http=Http("GET", "/robots/{robot}/state"),
        cli=Cli((), "state", args=("robot",)),
    )
    async def robot_state(ctx: Any, params: StateParams) -> Echo:
        return Echo(**params.model_dump())

    @operation(
        description="Send the robot home.",
        params=DockParams,
        result=CommandResult,
        capability=Capability.DOCK,
        motion=True,
        http=Http("POST", "/robots/{robot}/commands/dock"),
        cli=Cli((), "dock", args=("robot",)),
    )
    async def dock(ctx: Any, params: DockParams) -> CommandResult:
        return CommandResult(
            robot_id=params.robot,
            command="dock",
            outcome="confirmed" if params.wait else "unconfirmed",
            state=StateSummary(activity=Activity.DOCKED),
            waited_ms=5,
        )

    @operation(
        description="Empty the bin.",
        params=DockParams,
        result=CommandResult,
        capability=Capability.EVAC,
        http=Http("POST", "/robots/{robot}/commands/evac"),
        cli=Cli((), "evac", args=("robot",)),
    )
    async def evac(ctx: Any, params: DockParams) -> CommandResult:
        raise KitError(
            ErrorCode.NOT_SUPPORTED,
            "Braava jet m6 does not support evac.",
            robot_id=params.robot,
            details={"profile": "braava_m6", "capability": "evac"},
        )

    @operation(
        description="List rooms.",
        params=StateParams,
        result=list[Room],
        read_only=True,
        http=Http("GET", "/robots/{robot}/rooms"),
        cli=Cli(("rooms",), "list", args=("robot",)),
    )
    async def rooms_list(ctx: Any, params: StateParams) -> list[Room]:
        return [Room(id="1", name="Kitchen")]

    @operation(
        description="Service health.",
        result=Health,
        read_only=True,
        http=Http("GET", "/health", auth=False),
        cli=Cli((), "health"),
    )
    async def health(ctx: Any, params: Any) -> Health:
        return Health(status="ok")

    @operation(
        description="Always fails unexpectedly.",
        result=Health,
        http=Http("GET", "/boom"),
        surfaces={"http", "mcp"},
    )
    async def boom(ctx: Any, params: Any) -> Health:
        raise RuntimeError(f"connect failed with password {LEAKY}")

    @operation(
        description="List tokens.",
        result=list[Token],
        cli=Cli(("token",), "list"),
        surfaces={"cli"},
    )
    async def token_list(ctx: Any, params: Any) -> list[Token]:
        return []


@pytest.fixture
def test_ops() -> Iterator[None]:
    load_operations()  # before isolating, so real operations stay in the real registry
    with registry.isolated():
        register_test_operations()
        yield


@pytest.fixture
async def ctx(tmp_kit_home: Any, test_ops: None) -> AsyncIterator[ServiceContext]:
    context = ServiceContext(settings=load_settings(), transport_factory=FakeTransportFactory())
    yield context


@asynccontextmanager
async def running(app: FastAPI) -> AsyncIterator[None]:
    """Run the app's lifespan in its own task, as a server would (anyio scopes need that)."""
    started, stop = asyncio.Event(), asyncio.Event()

    async def lifespan() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(lifespan())
    waiter = asyncio.create_task(started.wait())
    await asyncio.wait([task, waiter], return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    if task.done():
        task.result()  # startup failed: raise it here
    try:
        yield
    finally:
        stop.set()
        await task


@pytest.fixture
async def client(ctx: ServiceContext) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(ctx)
    async with running(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
def auth(ctx: ServiceContext) -> dict[str, str]:
    return {"Authorization": f"Bearer {ctx.tokens.create('test')}"}


def _request(op_name: str, params: dict[str, Any]) -> registry.HttpRequest:
    return registry.http_request(registry.get(op_name), params)


async def _send(
    client: httpx.AsyncClient, op_name: str, params: dict[str, Any], headers: dict[str, str]
) -> httpx.Response:
    req = _request(op_name, params)
    return await client.request(
        req.method, "/v1" + req.path, params=req.query, json=req.body, headers=headers
    )


async def test_get_path_and_repeated_query_params(client, auth):
    resp = await _send(client, "robot_state", {"robot": I5, "fields": ["a", "b"], "limit": 3}, auth)
    assert resp.status_code == 200
    assert resp.json() == {"robot": I5, "fields": ["a", "b"], "limit": 3}


async def test_single_list_value_and_defaults(client, auth):
    resp = await _send(client, "robot_state", {"robot": "Vacuum", "fields": ["a"]}, auth)
    assert resp.json() == {"robot": "Vacuum", "fields": ["a"], "limit": 10}


async def test_post_body_command_results(client, auth):
    ok = await _send(client, "dock", {"robot": I5}, auth)
    assert ok.status_code == 200 and ok.json()["outcome"] == "confirmed"
    sent = await _send(client, "dock", {"robot": I5, "wait": False}, auth)
    assert sent.status_code == 202
    assert sent.json()["outcome"] == "unconfirmed" and sent.json()["state"]["activity"] == "docked"


async def test_list_result(client, auth):
    resp = await _send(client, "rooms_list", {"robot": I5}, auth)
    assert resp.json() == [{"id": "1", "name": "Kitchen"}]


async def test_kit_error_envelope(client, auth):
    resp = await _send(client, "evac", {"robot": I5}, auth)
    assert resp.status_code == 422
    assert resp.json() == {
        "error": {
            "code": "not_supported",
            "message": "Braava jet m6 does not support evac.",
            "robot_id": I5,
            "details": {"profile": "braava_m6", "capability": "evac"},
        }
    }


@pytest.mark.parametrize(
    ("kwargs", "problem_param"),
    [
        ({"params": {"limit": "lots"}}, "limit"),
        ({"content": b"not json"}, None),
        ({"json": [1, 2]}, None),
    ],
)
async def test_invalid_requests_use_kit_envelope(client, auth, kwargs, problem_param):
    if "params" in kwargs:
        resp = await client.get(f"/v1/robots/{I5}/state", headers=auth, **kwargs)
    else:
        resp = await client.post(f"/v1/robots/{I5}/commands/dock", headers=auth, **kwargs)
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "invalid_request"
    if problem_param:
        assert error["details"]["errors"][0]["param"] == problem_param


async def test_wrong_body_type_is_invalid_request(client, auth):
    resp = await client.post(f"/v1/robots/{I5}/commands/dock", headers=auth, json={"wait": []})
    assert resp.status_code == 400 and resp.json()["error"]["code"] == "invalid_request"


async def test_unexpected_exception_is_generic_internal(client, auth, caplog):
    with caplog.at_level(logging.ERROR):
        resp = await client.get("/v1/boom", headers=auth)
    assert resp.status_code == 500
    assert resp.json() == {
        "error": {"code": "internal", "message": generate.INTERNAL_MESSAGE},
    }
    assert LEAKY not in resp.text
    assert "operation boom failed" in caplog.text


@pytest.mark.parametrize("header", [None, "Bearer nope", "Basic abc", "Bearer "])
async def test_auth_required(client, header):
    headers = {"Authorization": header} if header else {}
    resp = await client.get(f"/v1/robots/{I5}/state", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_revoked_token_rejected(client, ctx, auth):
    assert (await client.get(f"/v1/robots/{I5}/state", headers=auth)).status_code == 200
    ctx.tokens.revoke("test")
    assert (await client.get(f"/v1/robots/{I5}/state", headers=auth)).status_code == 401


async def test_health_is_open(client):
    resp = await client.get("/v1/health")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


async def test_cli_only_op_not_routed_and_unknown_paths_use_envelope(client, auth):
    resp = await client.get("/v1/token_list", headers=auth)
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "invalid_request"
    wrong_method = await client.delete("/v1/health")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "invalid_request"


async def test_lifespan_starts_and_stops_context(ctx):
    app = create_app(ctx)
    assert app.state.ctx is ctx and not ctx.running
    async with running(app):
        assert ctx.running
    assert not ctx.running


async def test_openapi_documents_real_schemas(client):
    resp = await client.get("/v1/openapi.json")
    assert resp.status_code == 200
    doc = resp.json()
    paths = doc["paths"]
    assert "/v1/token_list" not in paths
    get_state = paths["/v1/robots/{robot}/state"]["get"]
    assert get_state["operationId"] == "robot_state"
    params = {p["name"]: p for p in get_state["parameters"]}
    assert params["robot"]["in"] == "path" and params["robot"]["required"] is True
    assert params["fields"]["in"] == "query" and params["fields"]["required"] is False
    assert params["robot"]["description"] == "Robot ID or name"
    assert get_state["security"] == [{"bearer": []}]
    ok_schema = get_state["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok_schema["$ref"].endswith("/Echo")
    assert "422" not in get_state["responses"]

    dock = paths["/v1/robots/{robot}/commands/dock"]["post"]
    body = dock["requestBody"]["content"]["application/json"]["schema"]
    assert set(body["properties"]) == {"wait", "note"}
    assert "202" in dock["responses"]
    assert "Supported on: " in dock["description"] and "Moves the robot" in dock["description"]

    assert "security" not in paths["/v1/health"]["get"]
    schemas = doc["components"]["schemas"]
    assert {"Echo", "CommandResult", "StateSummary", "ErrorEnvelope"} <= set(schemas)
    assert doc["components"]["securitySchemes"]["bearer"]["scheme"] == "bearer"
    assert (await client.get("/v1/docs")).status_code == 200


async def test_stream_presenter_hook(client, auth):
    seen: list[Any] = []

    async def presenter(request: Request, params: BaseModel, ctx: ServiceContext):
        seen.append(params)
        if "text/event-stream" in request.headers.get("accept", ""):
            return PlainTextResponse("data: hi\n\n", media_type="text/event-stream")
        return None

    register_stream_presenter("robot_state", presenter)
    try:
        sse = await client.get(
            f"/v1/robots/{I5}/state", headers={**auth, "Accept": "text/event-stream"}
        )
        assert sse.text == "data: hi\n\n"
        plain = await client.get(f"/v1/robots/{I5}/state", headers=auth)
        assert plain.json()["robot"] == I5
        assert isinstance(seen[0], StateParams) and seen[0].robot == I5
        unauth = await client.get(f"/v1/robots/{I5}/state", headers={"Accept": "text/event-stream"})
        assert unauth.status_code == 401 and len(seen) == 2
    finally:
        stream_presenters.pop("robot_state")


_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


async def test_mcp_mount_requires_auth(client):
    resp = await client.post("/mcp", json=_INIT, headers=_MCP_HEADERS)
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "unauthorized"


async def test_mcp_mount_serves_with_token(client, auth):
    resp = await client.post("/mcp", json=_INIT, headers={**_MCP_HEADERS, **auth})
    assert resp.status_code == 200, resp.text
    assert "roomba-kit" in resp.text


async def test_mcp_tool_call_over_mount(client, auth):
    headers = {**_MCP_HEADERS, **auth}
    init = await client.post("/mcp", json=_INIT, headers=headers)
    headers["mcp-session-id"] = init.headers["mcp-session-id"]
    note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    await client.post("/mcp", json=note, headers=headers)
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "evac", "arguments": {"robot": I5}},
    }
    resp = await client.post("/mcp", json=call, headers=headers)
    assert resp.status_code == 200
    assert '"isError":true' in resp.text and "not_supported: Braava jet m6" in resp.text
