"""US6: cloud extras when available, and clean degradation (FR-041–FR-043, SC-004, V6)."""

import asyncio
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
RUNNING = "01M6YKFD5R2N93S8D9ZJS7KH6X"
"""The mission in `i5_timeline.jsonl`: not in cloud history yet."""


async def until(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
def fake_cloud() -> FakeCloud:
    return FakeCloud.from_fixtures(FIXTURES)


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
