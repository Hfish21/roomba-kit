"""US6: cloud extras when available, and clean degradation (V6)."""

import asyncio
import copy
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from roomba_kit.api.app import create_app
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import ConfigModel, RobotEntry, load_settings, save_config
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import Event, EventType
from roomba_kit.core.models import ConnectionState
from roomba_kit.service import cloud, cloud_data
from roomba_kit.service.app import ServiceContext
from roomba_kit.transport.fake import FakeCloud, FakeTransportFactory
from tests.unit.test_api import running

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"
PUBLISHED = "01M0J6GSQ8MB898573DF8CCQNN"
"""In the i5 history fixture, with its UMF in `i5_mission_umf.json`."""
RUNNING = "01M6YKFD5R2N93S8D9ZJS7KH6X"
"""The mission in `i5_timeline.jsonl`: not in cloud history yet."""


async def until(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


class SlowCloud(FakeCloud):
    """FakeCloud whose REST calls can be made to hang."""

    hang = False

    async def pmaps(self, blid):
        if self.hang:
            self.calls.append("pmaps (hanging)")
            await asyncio.Event().wait()
        return await super().pmaps(blid)


@pytest.fixture
def fake_cloud() -> FakeCloud:
    fake = SlowCloud.from_fixtures(FIXTURES)
    for rid, prefix in ((I5, "i5"), (M6, "m6")):
        doc = json.loads((FIXTURES / "captures" / f"{prefix}_floorplan_umf.json").read_text())
        header = doc["maps"][0]["map_header"]
        fake.umf_by_key[(rid, header["id"], header["version"])] = doc
    return fake


@pytest.fixture
def factory() -> FakeTransportFactory:
    return FakeTransportFactory.from_fixtures(FIXTURES)


@pytest.fixture
async def ctx(tmp_kit_home, factory, fake_cloud, monkeypatch) -> AsyncIterator[ServiceContext]:
    monkeypatch.setattr(cloud, "POLL_S", 0.02)
    monkeypatch.setattr(cloud, "BACKOFF_MIN_S", 0.02)
    monkeypatch.setattr(cloud, "BACKOFF_MAX_S", 0.05)
    monkeypatch.setattr(cloud_data, "RECONCILE_S", 0.02)
    monkeypatch.setattr(cloud_data, "RETRY_S", 0.02)
    monkeypatch.setattr(cloud_data, "CALL_TIMEOUT_S", 0.2)
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
    creds.set_cloud("owner@example.com", "cloud-pw")
    yield ServiceContext(
        settings=load_settings(),
        transport_factory=factory,
        cloud_factory=lambda username, password, country: fake_cloud,
        session_options={"backoff_min": 0.01, "backoff_max": 0.02},
    )


@pytest.fixture
async def client(ctx) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(ctx)
    async with running(app):
        await until(lambda: all(s.connection is ConnectionState.CONNECTED for s in ctx))
        await until(lambda: all(s.has_state for s in ctx))
        await until(lambda: ctx.cloud.status().status == "connected")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.headers["Authorization"] = f"Bearer {ctx.tokens.create('test')}"
            yield c


def collect(ctx: ServiceContext, *types: EventType) -> list[Event]:
    seen: list[Event] = []

    async def on_event(event: Event) -> None:
        seen.append(event)

    ctx.bus.subscribe(on_event, types=list(types))
    return seen


async def cloud_down(ctx: ServiceContext, fake: FakeCloud) -> None:
    fake.fail_everything = True
    ctx.cloud.report_failure(
        KitError(ErrorCode.CLOUD_UNAVAILABLE, "down", details={"reason": "connection_failed"})
    )
    await until(lambda: ctx.cloud.status().status == "unavailable")


async def timeline(ctx: ServiceContext, count: int) -> list[Event]:
    """Persisted `cloud.timeline` events, once there are at least `count`."""
    async with asyncio.timeout(3.0):
        while True:
            events, _ = await ctx.bus.read(types=[EventType.CLOUD_TIMELINE], limit=1000)
            if len(events) >= count:
                return events
            await asyncio.sleep(0.005)


def unavailable(response: httpx.Response) -> str:
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "cloud_unavailable"
    return error["details"]["reason"]


async def test_timeline_reports_become_cloud_timeline_events(client, ctx, fake_cloud):
    await timeline(ctx, 7)
    await asyncio.sleep(0.05)  # the 31 reports repeat events: no more may arrive
    seen = await timeline(ctx, 7)
    got = [(e.data["kind"], e.data["room_id"], e.data["dest"]) for e in seen]
    assert got == [
        ("start", None, None),
        ("travel", "7", "room"),
        ("room", "7", None),
        ("travel", None, "dock"),
        ("traversal", "1", None),
        ("evac", None, None),
        ("fin", None, None),
    ]
    assert {e.robot_id for e in seen} == {I5}
    assert {e.data["mission_id"] for e in seen} == {RUNNING}
    assert all(e.data["raw_type"] == e.data["kind"] for e in seen)
    # Both robots declare cloud.timeline (the m6's is unverified), so both are watched.
    watchers = cloud_data.timeline_watchers(ctx)
    assert watchers is not None and watchers.watching() == {I5, M6}
    # Unknown types are kept as `other`, with the raw type.
    fake_cloud.push_timeline(M6, {"mission_id": "M2", "event": [{"type": "binFull", "ts": 5}]})
    seen = await timeline(ctx, 8)
    assert seen[-1].robot_id == M6
    assert seen[-1].data == {
        "mission_id": "M2",
        "kind": "other",
        "room_id": None,
        "dest": None,
        "raw_type": "binFull",
    }


async def test_timeline_stops_while_the_cloud_is_down_and_resumes(client, ctx, fake_cloud):
    await timeline(ctx, 7)
    watchers = cloud_data.timeline_watchers(ctx)
    await cloud_down(ctx, fake_cloud)
    await until(lambda: watchers.watching() == set())
    fake_cloud.fail_everything = False
    await until(lambda: ctx.cloud.status().status == "connected")
    await until(lambda: watchers.watching() == {I5, M6})
    fake_cloud.push_timeline(I5, {"mission_id": "M3", "event": [{"type": "start", "ts": 9}]})
    seen = await timeline(ctx, 8)
    assert len(seen) == 8
    assert (seen[-1].data["mission_id"], seen[-1].data["kind"]) == ("M3", "start")


async def test_floor_plan_has_rooms_with_polygons(client):
    response = await client.get(f"/v1/robots/{I5}/floor-plan")
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["map"] == {"map_id": "FbKwAujERSazAALZRD_rEg", "version": "231231T204559"}
    rooms = {r["id"]: r for r in plan["rooms"]}
    assert set(rooms) == {"1", "3", "4", "5", "6", "7", "8"}
    assert rooms["7"]["name"] == "Laundry Room"
    assert len(rooms["1"]["polygon"]) == 2  # outer ring and one hole
    assert all(len(point) == 2 for ring in rooms["1"]["polygon"] for point in ring)
    assert plan["retrieved_at"]
    # By name, too; the m6's unnamed rooms fall back to "Room <id>".
    mop = (await client.get("/v1/robots/Mop/floor-plan")).json()
    names = {r["id"]: r["name"] for r in mop["rooms"]}
    assert names["1"] == "Kitchen" and names["0"] == "Room 0"


async def test_cloud_history_is_kit_shaped(client):
    response = await client.get(f"/v1/robots/{I5}/cloud-history", params={"limit": 5})
    assert response.status_code == 200, response.text
    missions = response.json()
    assert len(missions) == 5
    first = missions[0]
    assert first["mission_ref"] == "01M6YA3MD0EKS3N4WT7JEG4CBE"
    assert first["outcome"] == "manually_docked"
    assert first["map"] == {"map_id": "FbKwAujERSazAALZRD_rEg", "version": "261108T205633"}
    assert not {"missionId", "done", "pmaps_info", "timeline"} & set(first)


async def test_coverage_is_not_yet_published_then_points(client, fake_cloud):
    response = await client.get(f"/v1/robots/{I5}/missions/{RUNNING}/coverage")
    assert unavailable(response) == "not_yet_published"

    # The cloud publishes it minutes after docking.
    record = copy.deepcopy(fake_cloud.history_by_robot[I5][1])
    record["missionId"] = record["timeline"]["mission_id"] = RUNNING
    fake_cloud.history_by_robot[I5].insert(0, record)
    response = await client.get(f"/v1/robots/{I5}/missions/{RUNNING}/coverage")
    assert response.status_code == 200, response.text
    coverage = response.json()
    assert coverage["mission_ref"] == RUNNING
    assert (coverage["map_id"], coverage["map_version"]) == (
        "iQpmRZnFReKgtSH8YpG4UA",
        "260821T153709",
    )
    assert len(coverage["points"]) > 100 and coverage["point_area_m2"] > 0
    assert coverage["dirt"] and coverage["clutter"]
    assert all(len(p) == 2 for p in coverage["points"] + coverage["dirt"] + coverage["clutter"])


async def test_cloud_down_cache_and_local_ops_unaffected(client, ctx, fake_cloud, factory):
    plan = (await client.get(f"/v1/robots/{I5}/floor-plan")).json()
    history = (await client.get(f"/v1/robots/{I5}/cloud-history")).json()
    coverage = (await client.get(f"/v1/robots/{I5}/missions/{PUBLISHED}/coverage")).json()

    await cloud_down(ctx, fake_cloud)
    # Cached results are served with their original retrieval time.
    assert (await client.get(f"/v1/robots/{I5}/floor-plan")).json() == plan
    cached = await client.get(f"/v1/robots/{I5}/missions/{PUBLISHED}/coverage")
    assert cached.json() == coverage
    # History isn't served stale: FR-043 exempts only rooms and floor plans.
    assert history and unavailable(await client.get(f"/v1/robots/{I5}/cloud-history")) == (
        "connection_failed"
    )
    # Nothing cached: cloud_unavailable with the reason.
    assert unavailable(await client.get(f"/v1/robots/{M6}/floor-plan")) == "connection_failed"
    response = await client.get(f"/v1/robots/{I5}/missions/{RUNNING}/coverage")
    assert unavailable(response) == "connection_failed"

    # Local commands, state and events are unaffected and fast.
    state_events = collect(ctx, EventType.STATE_CHANGED)
    factory.get(I5).emit({"batPct": 42})
    await until(lambda: ctx.session(I5).state.battery_pct == 42)
    await until(lambda: state_events)
    started = time.monotonic()
    result = await ctx.session(I5).execute("find")
    assert result.outcome == "confirmed"
    assert time.monotonic() - started < 0.5
    response = await client.get(f"/v1/robots/{M6}/position")
    assert response.status_code == 200


async def test_a_hanging_cloud_never_delays_local_commands(client, ctx, fake_cloud):
    fake_cloud.hang = True
    pending = asyncio.create_task(client.get(f"/v1/robots/{I5}/floor-plan"))
    await until(lambda: "pmaps (hanging)" in fake_cloud.calls)
    started = time.monotonic()
    assert (await ctx.session(I5).execute("find")).outcome == "confirmed"
    assert time.monotonic() - started < 0.5
    assert not pending.done()
    assert unavailable(await pending) == "timeout"
