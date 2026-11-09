"""US8 (quickstart V8): a local record of every mission that survives restarts (T085)."""

import asyncio
import itertools
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
from roomba_kit.transport.fake import FakeTransport, FakeTransportFactory

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"
START = 1794200000  # epoch seconds; each mission starts an hour after the last
_tokens = itertools.count()


async def until(predicate: Any, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
def factory(tmp_kit_home: Path) -> FakeTransportFactory:
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
    return FakeTransportFactory.from_fixtures(FIXTURES, reaction_delay=0.01)


@asynccontextmanager
async def serving(factory: FakeTransportFactory) -> AsyncIterator[httpx.AsyncClient]:
    """A fresh service on the same state dir, its lifespan in its own task, as `serve` runs it."""
    ctx = ServiceContext(
        settings=load_settings(),
        transport_factory=factory,
        session_options={"backoff_min": 0.01, "backoff_max": 0.02},
    )
    app = create_app(ctx)
    started, stop = asyncio.Event(), asyncio.Event()

    async def lifespan() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(lifespan())
    async with asyncio.timeout(3):
        while not (started.is_set() or task.done()):
            await asyncio.sleep(0.005)
    if task.done():
        task.result()
    headers = {"Authorization": f"Bearer {ctx.tokens.create(f'test{next(_tokens)}')}"}
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test/v1", headers=headers
        ) as client:

            async def ready() -> bool:
                return all(s.connection is ConnectionState.CONNECTED and s.has_state for s in ctx)

            await until(ready)
            client.ctx = ctx  # type: ignore[attr-defined]
            yield client
    finally:
        stop.set()
        await task


def emit_status(fake: FakeTransport, phase: str, cycle: str, mission_id: str, **extra: Any) -> None:
    status = fake.reported["cleanMissionStatus"]
    fake.emit(
        {
            "cleanMissionStatus": {
                **status,
                "phase": phase,
                "cycle": cycle,
                "missionId": mission_id,
                **extra,
            }
        }
    )


def echo(command: str, at: int, initiator: str = "localApp", **extra: Any) -> dict[str, Any]:
    return {"lastCommand": {"command": command, "time": at, "initiator": initiator, **extra}}


def begin(fake: FakeTransport, mission_id: str, n: int, initiator: str = "localApp") -> None:
    at = START + 3600 * n
    status = {"mssnStrtTm": at, "initiator": initiator}
    emit_status(fake, "charge", "clean", fake.reported["cleanMissionStatus"]["missionId"], **status)
    emit_status(fake, "run", "clean", mission_id, **status)


async def missions(client: httpx.AsyncClient, **query: Any) -> list[dict[str, Any]]:
    response = await client.get("/missions", params=query)
    assert response.status_code == 200, response.text
    return list(response.json())


async def wait_for(client: httpx.AsyncClient, mission_id: str, outcome: str) -> dict[str, Any]:
    async def done() -> bool:
        response = await client.get(f"/missions/{mission_id}")
        return response.status_code == 200 and response.json()["outcome"] == outcome

    await until(done)
    return dict((await client.get(f"/missions/{mission_id}")).json())


async def test_two_missions_survive_a_restart_and_filter(factory: FakeTransportFactory) -> None:
    fake = factory.get(I5)
    async with serving(factory) as client:
        # A kit room clean that finishes on the dock.
        fake.emit(echo("start", START, regions=[{"region_id": "7", "type": "rid"}]))
        begin(fake, "01MISSIONA0000000000000000", 0)
        emit_status(fake, "hmPostMsn", "clean", "01MISSIONA0000000000000000")
        emit_status(fake, "charge", "none", "01MISSIONA0000000000000000")
        await wait_for(client, "01MISSIONA0000000000000000", "completed")
        # A scheduled whole-home clean, stopped part way.
        begin(fake, "01MISSIONB0000000000000000", 1, initiator="schedule")
        fake.emit(echo("stop", START + 3700))
        emit_status(fake, "charge", "none", "01MISSIONB0000000000000000")
        await wait_for(client, "01MISSIONB0000000000000000", "cancelled")

    async with serving(factory) as client:
        a, b = "01MISSIONA0000000000000000", "01MISSIONB0000000000000000"
        listed = await missions(client)
        assert [m["id"] for m in listed] == [b, a]  # newest first
        second, first = listed
        assert first["robot_id"] == I5 and first["initiator"] == "kit"
        assert first["outcome"] == "completed" and first["rooms_requested"] == ["7"]
        assert first["ended_at"] and first["duration_s"] >= 0
        assert second["initiator"] == "schedule" and second["outcome"] == "cancelled"
        assert second["rooms_requested"] == []

        assert [m["id"] for m in await missions(client, robot="Vacuum")] == [b, a]
        assert await missions(client, robot="Mop") == []
        assert [m["id"] for m in await missions(client, outcome="completed")] == [a]
        since = "2026-11-08T00:00:00Z"
        assert [m["id"] for m in await missions(client, since=first["started_at"])] == [b, a]
        assert [m["id"] for m in await missions(client, until=second["started_at"])] == [a]
        assert await missions(client, since=since, until=since) == []
        assert [m["id"] for m in await missions(client, limit=1)] == [b]

        assert (await client.get(f"/missions/{a}")).json() == first
        unknown = await client.get("/missions/NOPE")
        assert unknown.status_code == 400
        assert unknown.json()["error"]["code"] == "invalid_request"
        bad = await client.get("/missions", params={"outcome": "exploded"})
        assert bad.status_code == 400


async def test_restart_mid_mission_continues_the_same_record(
    factory: FakeTransportFactory,
) -> None:
    fake = factory.get(I5)
    mission_id = "01MISSIONC0000000000000000"
    async with serving(factory) as client:
        begin(fake, mission_id, 2)
        open_ = await wait_for(client, mission_id, "in_progress")
        fake.emit({"cleanMissionStatus": {**fake.reported["cleanMissionStatus"], "error": 17}})

        async def recorded_error() -> bool:
            return bool((await client.get(f"/missions/{mission_id}")).json()["errors"])

        await until(recorded_error)
        fake.emit({"cleanMissionStatus": {**fake.reported["cleanMissionStatus"], "error": 0}})

    # The robot keeps cleaning while the service is down, then comes back under the same ID.
    async with serving(factory) as client:
        ctx = client.ctx  # type: ignore[attr-defined]
        emit_status(fake, "hmPostMsn", "clean", mission_id)
        emit_status(fake, "charge", "none", mission_id)
        done = await wait_for(client, mission_id, "completed")
        assert done["started_at"] == open_["started_at"]
        assert [e["code"] for e in done["errors"]] == [17]  # raised before the restart
        assert [m["id"] for m in await missions(client)] == [mission_id]
        events, _ = await ctx.bus.read(0, robot=I5, limit=1000)
        ids = [(e.type, e.data["mission"]["id"]) for e in events if "mission" in e.data]
        assert ids == [
            (EventType.MISSION_STARTED, mission_id),  # once, before the restart
            (EventType.MISSION_FINISHED, mission_id),
        ]
