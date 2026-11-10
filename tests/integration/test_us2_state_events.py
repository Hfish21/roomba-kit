"""US2: full state on demand and a live, resumable event stream."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from roomba_kit.api.app import create_app
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import ConfigModel, RobotEntry, load_settings, save_config
from roomba_kit.core.events import EventType
from roomba_kit.core.models import Activity, ConnectionState
from roomba_kit.service import alerts, derive
from roomba_kit.service.app import ServiceContext
from roomba_kit.transport.fake import FakeTransportFactory

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"


async def until(predicate: Any, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
def factory() -> FakeTransportFactory:
    return FakeTransportFactory.from_fixtures(FIXTURES, reaction_delay=0.01)


@pytest.fixture
async def ctx(tmp_kit_home: Path, factory: FakeTransportFactory) -> AsyncIterator[ServiceContext]:
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
    yield ServiceContext(
        settings=load_settings(),
        transport_factory=factory,
        session_options={"backoff_min": 0.01, "backoff_max": 0.02},
    )


@asynccontextmanager
async def serving(ctx: ServiceContext) -> AsyncIterator[httpx.AsyncClient]:
    """The app with its lifespan running in its own task, as a server would run it."""
    app = create_app(ctx)
    started, stop = asyncio.Event(), asyncio.Event()

    async def lifespan() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(lifespan())
    await until(lambda: started.is_set() or task.done())
    if task.done():
        task.result()
    headers = {"Authorization": f"Bearer {ctx.tokens.create('test')}"}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test/v1", headers=headers
        ) as client:
            await until(lambda: all(s.connection is ConnectionState.CONNECTED for s in ctx))
            await until(lambda: all(s.has_state for s in ctx))
            yield client
    finally:
        stop.set()
        await task


async def events_of(ctx: ServiceContext, robot: str, since: int = 0) -> list[Any]:
    events, _ = await ctx.bus.read(since, robot=robot, limit=1000)
    return events


async def test_state_reports_every_field(ctx: ServiceContext) -> None:
    async with serving(ctx) as client:
        body = (await client.get("/robots/Vacuum/state")).json()
        assert body["robot_id"] == I5 and body["activity"] in ("docked", "charging")
        assert isinstance(body["battery_pct"], int)
        assert body["bin"] == {"present": True, "full": False}
        assert body["clean_base"]["present"] is True
        assert body["signal"]["rssi"] is not None and body["map"]["map_id"]
        assert body["counters"]["missions_ok"] > 0
        assert body["last_command"]["room_ids"] == ["7"]
        mop = (await client.get(f"/robots/{M6}/state")).json()
        assert mop["tank"] is not None and mop["pad"] is not None and mop["bin"] is None
        missing = await client.get("/robots/Nobody/state")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "robot_not_found"


async def test_replayed_mission_produces_the_event_sequence(
    ctx: ServiceContext, factory: FakeTransportFactory
) -> None:
    fake = factory.get(I5)
    async with serving(ctx):
        since = ctx.bus.last_seq
        status = fake.reported["cleanMissionStatus"]
        for phase, cycle, mission_id in [
            ("charge", "clean", status["missionId"]),  # start transient, previous mission's ID
            ("run", "clean", "01NEWMISSION0000000000000A"),
            ("hmPostMsn", "clean", "01NEWMISSION0000000000000A"),
            ("charge", "none", "01NEWMISSION0000000000000A"),
        ]:
            fields = {"phase": phase, "cycle": cycle, "missionId": mission_id}
            fake.emit({"cleanMissionStatus": {**status, **fields}})
        wanted = [
            EventType.MISSION_STARTED,
            EventType.ACTIVITY_CHANGED,
            EventType.ACTIVITY_CHANGED,
            EventType.ACTIVITY_CHANGED,
            EventType.ROBOT_DOCKED,
            EventType.MISSION_FINISHED,
        ]

        async def derived() -> list[Any]:
            return [e for e in await events_of(ctx, I5, since) if e.type in wanted]

        async with asyncio.timeout(3):
            while len(await derived()) < len(wanted):
                await asyncio.sleep(0.01)
        events = await derived()
        assert [e.type for e in events] == wanted
        assert [e.data["to"] for e in events if e.type is EventType.ACTIVITY_CHANGED] == [
            "cleaning",
            "returning",
            "docked",
        ]
        finished = events[-1].data["mission"]
        assert finished["id"] == "01NEWMISSION0000000000000A"
        assert finished["outcome"] == "completed" and finished["initiator"] == "kit"


async def test_error_codes_carry_text(ctx: ServiceContext, factory: FakeTransportFactory) -> None:
    async with serving(ctx) as client:
        since = ctx.bus.last_seq
        status = factory.get(I5).reported["cleanMissionStatus"]
        factory.get(I5).emit({"cleanMissionStatus": {**status, "notReady": 68}})
        await until(lambda: ctx.sessions[I5].state.not_ready is not None)
        body = (await client.get(f"/robots/{I5}/state")).json()
        assert body["not_ready"] == {
            "code": 68,
            "text": "Not ready yet (transient around docking and bin emptying)",
        }
        params = {"since": since, "types": ["error.raised"]}
        # State updates before the derived event is published; wait for the event itself.
        page = (await client.get("/events/wait", params={**params, "timeout_s": 5})).json()
        assert [e["data"] for e in page["events"]] == [
            {
                "kind": "not_ready",
                "code": 68,
                "text": "Not ready yet (transient around docking and bin emptying)",
            }
        ]
        assert page["next"] == page["events"][-1]["seq"]
        bad = await client.get("/events", params={"types": ["robot.exploded"]})
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_request"


async def test_drop_and_restore_produce_connection_events(
    ctx: ServiceContext, factory: FakeTransportFactory
) -> None:
    async with serving(ctx) as client:
        since = ctx.bus.last_seq
        factory.get(M6).drop()
        await until(lambda: ctx.sessions[M6].connection is ConnectionState.OFFLINE)
        factory.get(M6).restore()
        await until(lambda: ctx.sessions[M6].connection is ConnectionState.CONNECTED)
        params = {"since": since, "robot": "Mop", "types": ["connection.changed"]}
        page = (await client.get("/events", params=params)).json()
        hops = [(e["data"]["from"], e["data"]["to"]) for e in page["events"]]
        assert ("connected", "offline") in hops and hops[-1] == ("connecting", "connected")
        assert all(e["robot_id"] == M6 for e in page["events"])


async def test_events_wait_resumes_from_since(
    ctx: ServiceContext, factory: FakeTransportFactory
) -> None:
    fake = factory.get(I5)
    async with serving(ctx) as client:
        head = ctx.bus.last_seq
        params = {"since": head, "robot": I5, "types": ["error.raised"]}
        empty = await client.get("/events/wait", params={**params, "timeout_s": 0.05})
        assert empty.json() == {"events": [], "next": head}

        waiter = asyncio.create_task(client.get("/events/wait", params=params))
        fake.emit({"batPct": 42})  # not an error: the waiter keeps waiting
        await until(lambda: ctx.sessions[I5].state.battery_pct == 42)
        await asyncio.sleep(0.05)
        assert not waiter.done()
        fake.emit({"cleanMissionStatus": {**fake.reported["cleanMissionStatus"], "error": 18}})
        page = (await asyncio.wait_for(waiter, 3)).json()
        assert [e["data"]["code"] for e in page["events"]] == [18]

        # A consumer that was away resumes from its last seq and misses nothing.
        caught_up = (await client.get("/events/wait", params={"since": head})).json()
        assert {"error.raised"} <= {e["type"] for e in caught_up["events"]}
        assert [e["seq"] for e in caught_up["events"]] == sorted(
            e["seq"] for e in caught_up["events"]
        )
        nxt = caught_up["next"]
        later = await client.get("/events/wait", params={"since": nxt, "timeout_s": 0.05})
        assert all(e["seq"] > nxt for e in later.json()["events"])


async def test_mission_wait_returns_the_terminal_mission(
    ctx: ServiceContext, factory: FakeTransportFactory
) -> None:
    fake = factory.get(I5)
    async with serving(ctx) as client:
        timed_out = await client.get(f"/robots/{I5}/missions/wait", params={"timeout_s": 0.05})
        assert timed_out.json() == {"timeout": True, "mission": None}
        waiter = asyncio.create_task(client.get("/robots/Vacuum/missions/wait"))
        status = fake.reported["cleanMissionStatus"]
        fake.emit({"cleanMissionStatus": {**status, "phase": "run", "cycle": "clean"}})
        await until(lambda: derive.current_mission(ctx, I5) is not None)
        mission_id = derive.current_mission(ctx, I5).id
        fake.emit({"cleanMissionStatus": {**status, "phase": "stop", "cycle": "none"}})
        mission = (await asyncio.wait_for(waiter, 3)).json()
        assert mission["id"] == mission_id and mission["outcome"] == "cancelled"
        again = await client.get(f"/robots/{I5}/missions/wait", params={"mission_id": mission_id})
        assert again.json() == mission
        unknown = await client.get(f"/robots/{I5}/missions/wait", params={"mission_id": "nope"})
        assert unknown.status_code == 400


async def test_dock_search_alert_fires_once_after_the_threshold(
    ctx: ServiceContext, factory: FakeTransportFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    monkeypatch.setattr(alerts, "clock", lambda: now[0])
    monkeypatch.setattr(alerts, "CHECK_INTERVAL_S", 0.01)
    fake = factory.get(I5)
    async with serving(ctx):
        status = fake.reported["cleanMissionStatus"]
        fake.emit({"cleanMissionStatus": {**status, "phase": "run", "cycle": "clean"}})
        fake.emit({"cleanMissionStatus": {**status, "phase": "hmPostMsn", "cycle": "clean"}})
        await until(lambda: ctx.sessions[I5].state.activity is Activity.RETURNING)
        mission = derive.current_mission(ctx, I5)
        since = ctx.bus.last_seq
        types = [EventType.ALERT_DOCK_SEARCH_TIMEOUT]

        async def fired() -> list[Any]:
            return (await ctx.bus.read(since, I5, types))[0]

        now[0] = 4 * 60
        await asyncio.sleep(0.05)
        assert not await fired()
        now[0] = 5 * 60 + 1  # past the default 5 minutes
        async with asyncio.timeout(3):
            while not await fired():
                await asyncio.sleep(0.01)
        now[0] = 30 * 60
        await asyncio.sleep(0.05)
        (alert,) = await fired()
        assert alert.data == {"mission_id": mission.id, "minutes": 5}
