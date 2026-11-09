import inspect
import json
import sys
import typing
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

import roomba_kit
from roomba_kit import client as client_module
from roomba_kit.client import Client
from roomba_kit.core import registry
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import EventType
from roomba_kit.core.models import CommandResult, Robot, RobotState
from roomba_kit.service.operations import load_operations
from roomba_kit.transport.fake import FakeTransportFactory
from tests.integration._us4 import FIXTURES, I5, M6, make_ctx, serving

GENERIC = {"request", "events", "aclose"}


def http_ops() -> list[registry.Operation]:
    load_operations()
    return [op for op in registry.all() if op.http is not None]


def hints(method: Any) -> dict[str, Any]:
    """Resolve the client's annotations, including its annotation-only (TYPE_CHECKING) imports."""
    namespace: dict[str, Any] = {}
    for name, module in sys.modules.items():
        if name.startswith("roomba_kit.service.operations."):
            namespace.update(vars(module))
    namespace.update(vars(client_module))
    return typing.get_type_hints(method, globalns=namespace)


def test_client_is_exported():
    assert roomba_kit.Client is Client
    assert "Client" in roomba_kit.__all__


@pytest.mark.parametrize("op", http_ops(), ids=lambda op: op.name)
def test_one_typed_method_per_http_operation(op):
    method = getattr(Client, op.name, None)
    assert method is not None, f"Client has no method for {op.name}: add one in client.py"
    params = list(inspect.signature(method).parameters.values())[1:]
    fields = op.params.model_fields
    assert [p.name for p in params] == list(fields), op.name
    for p in params:
        if fields[p.name].is_required():
            assert p.default is inspect.Parameter.empty, (op.name, p.name)
        else:
            assert p.default is None, f"{op.name}.{p.name}: optional params default to None"
    resolved = hints(method)
    assert resolved["return"] == op.result, op.name
    for p in params:
        expected = fields[p.name].annotation
        if not fields[p.name].is_required():
            expected = expected | None
        assert resolved[p.name] == expected, (op.name, p.name)


def test_no_method_without_an_operation():
    names = {op.name for op in http_ops()}
    public = {n for n, v in vars(Client).items() if callable(v) and not n.startswith("_")}
    assert public - GENERIC <= names


@pytest.fixture
async def served(tmp_kit_home):
    factory = FakeTransportFactory.from_fixtures(FIXTURES, reaction_delay=0.01)
    async with serving(make_ctx(factory)) as s:
        yield s


async def test_typed_results_and_errors_against_the_api(served):
    async with Client("http://test", served.token, transport=served.transport) as kit:
        robots = await kit.list_robots()
        assert [type(r) for r in robots] == [Robot, Robot]
        assert {r.id for r in robots} == {I5, M6}
        state = await kit.get_state("Vacuum")
        assert isinstance(state, RobotState) and state.robot_id == I5
        expected = await served.client.get("/v1/robots/Vacuum/state", headers=served.auth)
        assert state.model_dump(mode="json", by_alias=True) == expected.json()
        with pytest.raises(KitError) as info:
            await kit.evac("Mop")
        assert info.value.code is ErrorCode.NOT_SUPPORTED and info.value.robot_id == M6
        with pytest.raises(KitError) as info:
            await kit.get_state("Nobody")
        assert info.value.code is ErrorCode.ROBOT_NOT_FOUND
        page = await kit.events_list(since=0, limit=2)
        assert len(page.events) <= 2
        assert (await kit.health()).status == "ok"


async def test_unconfirmed_is_a_result_not_an_error():
    body = {
        "robot_id": I5,
        "command": "dock",
        "outcome": "unconfirmed",
        "state": {"activity": "cleaning"},
        "waited_ms": 10000,
    }
    transport = httpx.MockTransport(lambda req: httpx.Response(202, json=body))
    async with Client("http://svc", "rk_test", transport=transport) as kit:
        result = await kit.dock("Vacuum")
    assert isinstance(result, CommandResult) and result.outcome == "unconfirmed"


async def test_requests_carry_the_token_and_drop_unset_params():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"events": [], "next": 5})

    async with Client("http://svc/", "rk_test", transport=httpx.MockTransport(handler)) as kit:
        await kit.events_wait(5, types=[EventType.MISSION_FINISHED, EventType.ERROR_RAISED])
    (req,) = seen
    assert req.headers["authorization"] == "Bearer rk_test"
    assert (
        str(req.url)
        == "http://svc/v1/events/wait?since=5&types=mission.finished&types=error.raised"
    )


async def test_datetime_and_enum_params_go_as_their_json_form():
    import datetime as dt

    from roomba_kit.core.models import MissionOutcome

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    async with Client("http://svc", "rk_test", transport=httpx.MockTransport(handler)) as kit:
        since = dt.datetime(2026, 11, 1, tzinfo=dt.UTC)
        assert await kit.list_missions(since=since, outcome=MissionOutcome.COMPLETED) == []
    assert dict(seen[0].url.params) == {"since": "2026-11-01T00:00:00Z", "outcome": "completed"}


async def test_invalid_params_fail_locally():
    transport = httpx.MockTransport(lambda req: pytest.fail("sent"))
    async with Client("http://svc", "rk_test", transport=transport) as kit:
        with pytest.raises(KitError) as info:
            await kit.events_list(limit=0)
    assert info.value.code is ErrorCode.INVALID_REQUEST


async def test_service_down_is_service_unavailable():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with Client("http://svc", "rk_test", transport=httpx.MockTransport(refuse)) as kit:
        with pytest.raises(KitError) as info:
            await kit.list_robots()
    assert info.value.code is ErrorCode.SERVICE_UNAVAILABLE


def frame(seq: int) -> bytes:
    envelope = {
        "schema": "roomba-kit.event/v1",
        "seq": seq,
        "type": "bin.full",
        "robot_id": I5,
        "at": "2026-11-08T12:00:00Z",
        "data": {"full": True},
    }
    return f"id: {seq}\r\nevent: bin.full\r\ndata: {json.dumps(envelope)}\r\n\r\n".encode()


class Stream:
    """SSE connections: each entry is the frames one connection sends before it drops."""

    def __init__(self, *connections: list[bytes]) -> None:
        self.connections = list(connections)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        frames = self.connections.pop(0) if self.connections else []

        async def body() -> AsyncIterator[bytes]:
            yield b": ping\r\n\r\n"
            for f in frames:
                yield f
            if not self.connections and not frames:
                raise httpx.ReadError("gone")

        headers = {"content-type": "text/event-stream; charset=utf-8"}
        return httpx.Response(200, headers=headers, content=body())


async def test_events_resume_after_a_drop_with_last_event_id():
    stream = Stream([frame(4), frame(5)], [], [frame(6)])
    got = []
    async with Client("http://svc", "rk_test", transport=httpx.MockTransport(stream)) as kit:
        async for event in kit.events(3, robot="Vacuum", types=[EventType.BIN_FULL], retry_s=0):
            got.append(event)
            if len(got) == 3:
                break
    assert [e.seq for e in got] == [4, 5, 6]
    assert got[0].type is EventType.BIN_FULL and got[0].data == {"full": True}
    assert [r.headers.get("last-event-id") for r in stream.requests] == ["3", "5", "5"]
    assert all(r.headers["accept"] == "text/event-stream" for r in stream.requests)
    assert stream.requests[0].url.params.get_list("types") == ["bin.full"]
    assert stream.requests[0].url.params["robot"] == "Vacuum"


async def test_events_start_live_without_since():
    stream = Stream([frame(9)])
    async with Client("http://svc", "rk_test", transport=httpx.MockTransport(stream)) as kit:
        async for event in kit.events(retry_s=0):
            assert event.seq == 9
            break
    assert "last-event-id" not in stream.requests[0].headers


async def test_events_raise_service_errors_instead_of_retrying():
    body = {"error": {"code": "unauthorized", "message": "Missing or invalid API token."}}
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, json=body)

    async with Client("http://svc", "rk_bad", transport=httpx.MockTransport(handler)) as kit:
        with pytest.raises(KitError) as info:
            async for _ in kit.events(retry_s=0):
                pass
    assert info.value.code is ErrorCode.UNAUTHORIZED and len(calls) == 1


async def test_model_params_are_sent_as_json():
    from roomba_kit.service.operations.rooms import RoomOptions

    seen: list[httpx.Request] = []
    body = {
        "robot_id": I5,
        "command": "clean_rooms",
        "outcome": "confirmed",
        "state": {"activity": "cleaning"},
        "waited_ms": 10,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    async with Client("http://svc", "rk_test", transport=httpx.MockTransport(handler)) as kit:
        options = {"Office": RoomOptions(passes="two")}
        await kit.clean_rooms("Vacuum", ["Office", "7"], room_options=options)
    assert json.loads(seen[0].content) == {
        "rooms": ["Office", "7"],
        "room_options": {"Office": {"passes": "two"}},
    }
