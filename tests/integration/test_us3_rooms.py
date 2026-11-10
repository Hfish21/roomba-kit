"""US3: clean specific rooms by name."""

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


async def clean(client: httpx.AsyncClient, robot: str, **body) -> httpx.Response:
    return await client.post(f"/v1/robots/{robot}/commands/clean_rooms", json=body)


def start_commands(factory: FakeTransportFactory, robot: str) -> list[dict | None]:
    return [params for name, params in factory.get(robot).sent_commands if name == "start"]


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


async def test_clean_rooms_by_name_sends_regions_in_order(client, factory):
    await client.post(f"/v1/robots/{I5}/rooms/import")
    response = await clean(client, "Vacuum", rooms=["Laundry Room", "office", "1"])
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "confirmed"
    assert start_commands(factory, I5)[-1] == {
        "pmap_id": I5_MAP,
        "user_pmapv_id": I5_VERSION,
        "ordered": 1,
        "regions": rid("7", "4", "1"),
    }

    await clean(client, I5, rooms=["Den"], ordered=False)
    assert start_commands(factory, I5)[-1]["ordered"] == 0


async def test_an_empty_room_list_is_rejected_before_anything_is_sent(client, factory):
    await client.post(f"/v1/robots/{I5}/rooms/import")
    response = await clean(client, I5, rooms=[])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert start_commands(factory, I5) == []


async def test_an_unknown_room_lists_the_valid_ones(client, factory):
    await client.post(f"/v1/robots/{I5}/rooms/import")
    response = await clean(client, I5, rooms=["Laundry Room", "Nowhere"])
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "room_not_found"
    assert {"id": "7", "name": "Laundry Room"} in error["details"]["valid"]
    assert start_commands(factory, I5) == []


async def test_a_changed_map_version_marks_stored_rooms_stale(client, ctx, factory):
    await client.post(f"/v1/robots/{I5}/rooms/import")
    factory.get(I5).emit({"pmaps": [{I5_MAP: "261201T090000"}]})
    await until(lambda: ctx.sessions[I5].state.map.version == "261201T090000")
    listed = (await client.get(f"/v1/robots/{I5}/rooms")).json()
    assert listed["stale"] is True
    assert listed["map"] == {"map_id": I5_MAP, "version": "261201T090000"}
    assert {r["map_version"] for r in listed["rooms"]} == {I5_VERSION}  # nothing learned since


async def test_a_changed_map_version_refuses_stale_names(client, ctx, factory):
    await client.post(f"/v1/robots/{I5}/rooms/import")
    factory.get(I5).emit({"pmaps": [{I5_MAP: "261201T090000"}]})
    await until(lambda: ctx.sessions[I5].state.map.version == "261201T090000")

    response = await clean(client, I5, rooms=["Laundry Room"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "map_changed"
    assert start_commands(factory, I5) == []

    factory.get(I5).emit({"pmaps": []})
    await until(lambda: ctx.sessions[I5].state.map is None)
    response = await clean(client, I5, rooms=["Laundry Room"])
    assert response.json()["error"]["code"] == "no_map"


async def test_imported_rooms_stay_usable_while_the_cloud_is_down(client, fake_cloud, factory):
    await client.post(f"/v1/robots/{I5}/rooms/import")
    fake_cloud.fail_everything = True
    failed = await client.post(f"/v1/robots/{I5}/rooms/import")
    assert failed.json()["error"]["code"] == "cloud_unavailable"
    listed = (await client.get(f"/v1/robots/{I5}/rooms")).json()
    assert len(listed["rooms"]) == 7 and listed["retrieved_at"]
    assert (await clean(client, I5, rooms=["Laundry Room"])).status_code == 200


@pytest.mark.parametrize("with_cloud", [False])
async def test_learned_rooms_are_captured_and_named_without_the_cloud(client, ctx, factory):
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

    named = await client.put(f"/v1/robots/{I5}/rooms/3", json={"name": "Den"})
    assert named.status_code == 200, named.text
    assert named.json()["name"] == "Den" and named.json()["local_name"] == "Den"

    response = await clean(client, I5, rooms=["den", "6"])
    assert response.status_code == 200, response.text
    sent = start_commands(factory, I5)[-1]
    assert sent["regions"] == rid("3", "6") and sent["pmap_id"] == I5_MAP

    # Learned IDs belong to the map version they were seen on, like imported ones.
    factory.get(I5).emit({"pmaps": [{I5_MAP: "261201T090000"}]})
    await until(lambda: ctx.sessions[I5].state.map.version == "261201T090000")
    for ref in ("den", "6"):
        response = await clean(client, I5, rooms=[ref])
        assert response.json()["error"]["code"] == "map_changed"
