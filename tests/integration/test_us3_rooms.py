"""US3: clean specific rooms by name (FR-008, FR-031, FR-032, FR-043, quickstart V3)."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from roomba_kit.api.app import create_app
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import ConfigModel, RobotEntry, load_settings, save_config
from roomba_kit.core.models import ConnectionState
from roomba_kit.service import cloud
from roomba_kit.service.app import ServiceContext
from roomba_kit.transport.fake import FakeCloud, FakeTransportFactory
from tests.unit.test_api import running

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"
I5_MAP, I5_VERSION = "FbKwAujERSazAALZRD_rEg", "231231T204559"
M6_MAP = "5brjBHI-RtKr1Yt7JzFjDw"


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def rid(*ids: str) -> list[dict]:
    return [{"region_id": i, "type": "rid"} for i in ids]


@pytest.fixture
def factory() -> FakeTransportFactory:
    return FakeTransportFactory.from_fixtures(FIXTURES)


@pytest.fixture
def fake_cloud() -> FakeCloud:
    return FakeCloud.from_fixtures(FIXTURES)


@pytest.fixture
def with_cloud() -> bool:
    return True


@pytest.fixture
async def ctx(tmp_kit_home, factory, fake_cloud, with_cloud, monkeypatch) -> ServiceContext:
    monkeypatch.setattr(cloud, "POLL_S", 0.02)
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
    if with_cloud:
        creds.set_cloud("owner@example.com", "cloud-pw")
    return ServiceContext(
        settings=load_settings(),
        transport_factory=factory,
        cloud_factory=lambda username, password, country: fake_cloud,
        session_options={"backoff_min": 0.01, "backoff_max": 0.02},
    )


@pytest.fixture
async def client(ctx, with_cloud) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(ctx)
    async with running(app):
        await until(lambda: all(s.connection is ConnectionState.CONNECTED for s in ctx))
        await until(lambda: all(s.has_state for s in ctx))
        if with_cloud:
            await until(lambda: ctx.cloud.status().status == "connected")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.headers["Authorization"] = f"Bearer {ctx.tokens.create('test')}"
            yield c


async def test_import_lists_rooms_with_placeholders_for_unnamed_ones(client):
    imported = await client.post(f"/v1/robots/{I5}/rooms/import")
    assert imported.status_code == 200, imported.text
    assert imported.json() == {"imported": 7, "map": {"map_id": I5_MAP, "version": I5_VERSION}}

    listed = (await client.get("/v1/robots/Vacuum/rooms")).json()
    assert listed["stale"] is False and listed["retrieved_at"]
    names = {r["room_id"]: r["name"] for r in listed["rooms"]}
    assert names["7"] == "Laundry Room" and names["1"] == "Living Room"
    assert "2" not in names  # there is no rid 2 on the i5

    assert (await client.post(f"/v1/robots/{M6}/rooms/import")).json()["imported"] == 6
    mop = (await client.get(f"/v1/robots/{M6}/rooms")).json()
    assert [(r["room_id"], r["name"]) for r in mop["rooms"]] == [
        ("0", "Room 0"),
        ("1", "Kitchen"),
        ("2", "Room 2"),
        ("3", "Room 3"),
        ("4", "Room 4"),
        ("5", "Room 5"),
    ]
    assert mop["rooms"][0]["cloud_name"] is None


async def test_a_changed_map_version_marks_stored_rooms_stale(client, ctx, factory):
    await client.post(f"/v1/robots/{I5}/rooms/import")
    factory.get(I5).emit({"pmaps": [{I5_MAP: "261201T090000"}]})
    await until(lambda: ctx.sessions[I5].state.map.version == "261201T090000")
    listed = (await client.get(f"/v1/robots/{I5}/rooms")).json()
    assert listed["stale"] is True
    assert listed["map"] == {"map_id": I5_MAP, "version": "261201T090000"}
    assert {r["map_version"] for r in listed["rooms"]} == {I5_VERSION}  # nothing learned since


async def test_imported_rooms_stay_usable_while_the_cloud_is_down(client, fake_cloud, factory):
    await client.post(f"/v1/robots/{I5}/rooms/import")
    fake_cloud.fail_everything = True
    failed = await client.post(f"/v1/robots/{I5}/rooms/import")
    assert failed.json()["error"]["code"] == "cloud_unavailable"
    listed = (await client.get(f"/v1/robots/{I5}/rooms")).json()
    assert len(listed["rooms"]) == 7 and listed["retrieved_at"]


@pytest.mark.parametrize("with_cloud", [False])
async def test_learned_rooms_are_captured_and_named_without_the_cloud(client, factory):
    imported = await client.post(f"/v1/robots/{I5}/rooms/import")
    assert imported.json()["error"]["code"] == "cloud_unavailable"

    # A room clean started by the iRobot app.
    factory.get(I5).emit(
        {
            "lastCommand": {
                "command": "start",
                "time": int(time.time()),
                "initiator": "rmtApp",
                "pmap_id": I5_MAP,
                "user_pmapv_id": I5_VERSION,
                "ordered": 1,
                "regions": rid("3", "6"),
            }
        }
    )

    async def learned() -> set[str]:
        rooms = (await client.get(f"/v1/robots/{I5}/rooms")).json()["rooms"]
        return {r["room_id"] for r in rooms}

    async with asyncio.timeout(2):
        while {"3", "6"} - await learned():
            await asyncio.sleep(0.01)
    assert "7" in await learned()  # from the room clean already echoed at startup
