"""US2 (quickstart V2): full state on demand and a live, resumable event stream (T048)."""

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
from roomba_kit.core.models import ConnectionState
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
