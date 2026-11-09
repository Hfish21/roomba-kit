"""US7: live position (FR-039, FR-040, research R13, quickstart V7)."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from roomba_kit.api.app import create_app
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import ConfigModel, RobotEntry, load_settings, save_config
from roomba_kit.core.events import Event, EventType
from roomba_kit.core.models import Activity, ConnectionState, PositionFix
from roomba_kit.service import position
from roomba_kit.service.app import ServiceContext
from roomba_kit.transport.fake import FakeTransportFactory
from tests.unit.test_api import running

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"
INTERVAL = 0.05


def cleaning(mission: str = "M1") -> dict:
    return {"cleanMissionStatus": {"cycle": "clean", "phase": "run", "missionId": mission}}


def paused(mission: str = "M1") -> dict:
    return {"cleanMissionStatus": {"cycle": "clean", "phase": "stop", "missionId": mission}}


DOCKED = {"cleanMissionStatus": {"cycle": "none", "phase": "charge"}, "batPct": 100}


def fix(x: float) -> PositionFix:
    return PositionFix(
        robot_id=M6,
        at=datetime.now(UTC),
        x_m=x,
        y_m=-0.5,
        heading_rad=3.1,
        map_id="map-1",
        map_version="v1",
    )


async def until(predicate, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
def factory() -> FakeTransportFactory:
    return FakeTransportFactory.from_fixtures(FIXTURES)


@pytest.fixture
def poll_timeout() -> float:
    return INTERVAL


@pytest.fixture
async def ctx(tmp_kit_home, factory, monkeypatch, poll_timeout) -> AsyncIterator[ServiceContext]:
    monkeypatch.setattr(position, "POLL_INTERVAL_S", INTERVAL)
    monkeypatch.setattr(position, "POLL_TIMEOUT_S", poll_timeout)
    save_config(
        ConfigModel(
            robots=[
                RobotEntry(id=I5, name="Vacuum", address="192.0.2.10", profile="roomba_i5"),
                RobotEntry(id=M6, name="Mop", address="192.0.2.11", profile="braava_m6"),
            ]
        )
    )
    creds = CredentialStore()
    creds.set_robot_password(I5, "pw1")
    creds.set_robot_password(M6, "pw2")
    context = ServiceContext(
        settings=load_settings(),
        transport_factory=factory,
        session_options={"backoff_min": 0.01, "backoff_max": 0.02},
    )
    yield context


@pytest.fixture
async def client(ctx) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(ctx)
    async with running(app):
        await until(lambda: all(s.connection is ConnectionState.CONNECTED for s in ctx))
        await until(lambda: all(s.has_state for s in ctx))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.headers["Authorization"] = f"Bearer {ctx.tokens.create('test')}"
            yield c


def collect(ctx: ServiceContext) -> list[tuple[float, Event]]:
    seen: list[tuple[float, Event]] = []
    loop = asyncio.get_running_loop()

    async def on_event(event: Event) -> None:
        seen.append((loop.time(), event))

    ctx.bus.subscribe(on_event, types=[EventType.POSITION])
    return seen


async def test_cleaning_m6_streams_fixes_at_the_poll_interval(client, ctx, factory):
    mop = factory.get(M6)
    mop.script_positions(*(fix(i / 10) for i in range(50)))
    seen = collect(ctx)
    mop.emit(cleaning())
    await until(lambda: len(seen) >= 4)
    times = [t for t, _ in seen]
    assert all(b - a <= INTERVAL * 3 for a, b in zip(times, times[1:], strict=False))
    data = seen[0][1].data
    assert seen[0][1].robot_id == M6
    assert {"robot_id", "at", "x_m", "y_m", "heading_rad", "map_id", "map_version"} <= set(data)
    assert data["map_id"] == "map-1"

    # Returning to dock still polls; docked stops it.
    mop.emit({"cleanMissionStatus": {"cycle": "clean", "phase": "hmUsrDock", "missionId": "M1"}})
    await until(lambda: ctx.session(M6).state.activity is Activity.RETURNING)
    count = len(seen)
    await until(lambda: len(seen) > count)
    mop.emit(DOCKED)
    await asyncio.sleep(INTERVAL * 2)
    requests = mop.position_requests
    await asyncio.sleep(INTERVAL * 4)
    assert mop.position_requests == requests


async def test_position_events_are_not_persisted(client, ctx, factory):
    mop = factory.get(M6)
    mop.script_positions(fix(1.0))
    seen = collect(ctx)
    mop.emit(cleaning())
    await until(lambda: seen)
    events, _ = await ctx.bus.read(0, types=[EventType.POSITION])
    assert events == []


async def test_docked_m6_answers_no_fix(client, ctx, factory):
    mop = factory.get(M6)
    await asyncio.sleep(INTERVAL * 3)
    assert mop.position_requests == 0  # docked: never polled
    resp = await client.get(f"/v1/robots/{M6}/position")
    assert resp.status_code == 200
    assert resp.json() == {"no_fix": True}
    assert mop.position_requests == 1


async def test_get_position_returns_a_fix_by_name(client, factory):
    factory.get(M6).script_positions(fix(2.5))
    resp = await client.get("/v1/robots/mop/position")
    assert resp.status_code == 200
    body = resp.json()
    assert body["x_m"] == 2.5 and body["robot_id"] == M6 and body["map_version"] == "v1"


async def test_i5_is_not_supported_without_a_transport_call(client, ctx, factory):
    vacuum = factory.get(I5)
    resp = await client.get(f"/v1/robots/{I5}/position")
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "not_supported"
    assert error["details"] == {"profile": "roomba_i5", "capability": "live_position"}
    vacuum.emit(cleaning())
    await until(lambda: ctx.session(I5).state.activity is Activity.CLEANING)
    await asyncio.sleep(INTERVAL * 4)
    assert vacuum.position_requests == 0


async def test_never_answering_robot_stops_after_three_timeouts(client, ctx, factory):
    mop = factory.get(M6)
    mop.positions_never_answer = True
    mop.emit(cleaning("M1"))
    await until(lambda: mop.position_requests >= 3)
    await asyncio.sleep(INTERVAL * 5)
    assert mop.position_requests == 3

    # Pausing and resuming the same mission doesn't restart polling.
    mop.emit(paused("M1"))
    mop.emit(cleaning("M1"))
    await asyncio.sleep(INTERVAL * 5)
    assert mop.position_requests == 3

    # A new mission tries again.
    mop.emit(DOCKED)
    mop.emit(cleaning("M2"))
    await until(lambda: mop.position_requests >= 6)
    await asyncio.sleep(INTERVAL * 5)
    assert mop.position_requests == 6


async def test_robot_that_has_answered_keeps_polling_through_timeouts(client, ctx, factory):
    mop = factory.get(M6)
    mop.script_positions(fix(0.0))
    seen = collect(ctx)
    mop.emit(cleaning())
    await until(lambda: seen)
    mop.positions_never_answer = True
    await until(lambda: mop.position_requests >= 6, timeout=3.0)


@pytest.mark.parametrize("poll_timeout", [1.0])
async def test_polling_never_delays_commands(client, ctx, factory):
    mop = factory.get(M6)
    mop.positions_never_answer = True
    mop.emit(cleaning("M3"))
    await until(lambda: mop.position_requests >= 1)  # a request is now in flight for 1 s

    async def job() -> str:
        return "ran"

    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await ctx.session(M6).submit(job) == "ran"
    assert loop.time() - started < 0.2
