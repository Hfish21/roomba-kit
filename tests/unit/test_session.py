import asyncio
from pathlib import Path

import pytest

from roomba_kit.config.settings import RobotEntry
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import EventType
from roomba_kit.core.models import Activity, ConnectionState
from roomba_kit.profiles.loader import get_profile
from roomba_kit.service.eventbus import EventBus
from roomba_kit.service.session import RobotSession
from roomba_kit.service.store import Store
from roomba_kit.transport.fake import FakeTransportFactory

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"


@pytest.fixture
async def bus(tmp_path):
    store = await Store(tmp_path / "state.db").open()
    bus = EventBus(store)
    await bus.start()
    yield bus
    await bus.close()
    await store.close()


@pytest.fixture
def factory():
    return FakeTransportFactory.from_fixtures(FIXTURES, reaction_delay=0.01)


async def _until(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def _session(bus, factory, robot_id=I5, profile="roomba_i5", password="pw", **kw):
    entry = RobotEntry(id=robot_id, name="Vacuum", address="192.0.2.10", profile=profile)
    kw.setdefault("backoff_min", 0.01)
    kw.setdefault("backoff_max", 0.04)
    kw.setdefault("coalesce_s", 0.05)
    return RobotSession(entry, get_profile(profile), factory, lambda: password, bus, **kw)


async def test_connects_and_normalizes(bus, factory):
    s = _session(bus, factory)
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.CONNECTED and s.has_state)
        assert s.state.activity in (Activity.DOCKED, Activity.CHARGING)
        assert s.robot().sku == "i5G5020"
    finally:
        await s.stop()


async def test_backoff_doubles_to_the_cap_then_rediscovers(bus, factory):
    fake = factory.get(I5)
    fake.drop(offline=True)  # every connect fails with robot_offline
    found = []

    async def rediscover(blid):
        found.append(blid)
        return "192.0.2.99"

    moved = []
    s = _session(bus, factory, rediscover=rediscover, on_address=lambda r, a: moved.append(a))
    await s.start()
    try:
        await _until(lambda: len(s.backoff_history) >= 5)
        assert s.backoff_history[:5] == [0.01, 0.02, 0.04, 0.04, 0.04]
        assert s.connection is ConnectionState.OFFLINE
        assert found == [I5] and moved == ["192.0.2.99"] and s.address == "192.0.2.99"
        fake.restore()
        await _until(lambda: s.connection is ConnectionState.CONNECTED)
    finally:
        await s.stop()


async def test_held_elsewhere_is_detected(bus, factory):
    factory.get(I5).fail_next_connect(ErrorCode.CONNECTION_HELD_ELSEWHERE)
    s = _session(bus, factory)
    seen = []

    async def record(event):
        seen.append(event.data["to"])

    bus.subscribe(record, types=[EventType.CONNECTION_CHANGED])
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.CONNECTED)
        await bus.drain()
        assert "held_elsewhere" in seen and seen[-1] == "connected"
    finally:
        await s.stop()


async def test_auth_failed_is_terminal_until_credentials_change(bus, factory):
    fake = factory.get(I5)
    fake.fail_next_connect(ErrorCode.AUTH_FAILED)
    s = _session(bus, factory)
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.AUTH_FAILED)
        await asyncio.sleep(0.1)
        assert fake.connect_calls == 1
        s.credentials_changed()
        await _until(lambda: s.connection is ConnectionState.CONNECTED)
    finally:
        await s.stop()


async def test_not_configured_without_password(bus, factory):
    s = _session(bus, factory, password=None)
    await s.start()
    try:
        await asyncio.sleep(0.05)
        assert s.connection is ConnectionState.NOT_CONFIGURED
        assert factory.get(I5).connect_calls == 0
        with pytest.raises(KitError) as exc:
            await s.execute("find")
        assert exc.value.code is ErrorCode.ROBOT_OFFLINE
    finally:
        await s.stop()


async def test_not_supported_raised_without_transport_call(bus, factory):
    s = _session(bus, factory, robot_id=M6, profile="braava_m6")
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.CONNECTED)
        with pytest.raises(KitError) as exc:
            await s.execute("evac")
        assert exc.value.code is ErrorCode.NOT_SUPPORTED
        assert exc.value.details == {"profile": "braava_m6", "capability": "evac"}
        assert factory.get(M6).sent_commands == []
    finally:
        await s.stop()


async def test_commands_run_fifo_and_confirm(bus, factory):
    s = _session(bus, factory)
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.CONNECTED and s.has_state)
        results = await asyncio.gather(
            s.execute("clean"), s.execute("pause"), s.execute("resume"), s.execute("find")
        )
        assert [r.outcome for r in results] == ["confirmed"] * 4
        assert [c for c, _ in factory.get(I5).sent_commands] == ["start", "pause", "resume", "find"]
    finally:
        await s.stop()


async def test_dropped_command_is_unconfirmed(bus, factory):
    from roomba_kit.profiles.base import CommandSpec
    from roomba_kit.service import confirm

    fake = factory.get(I5)
    fake.dropped_commands.add("pause")
    s = _session(bus, factory)
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.CONNECTED and s.has_state)
        await s.execute("clean")
        original = confirm.DEFAULT_SPECS["pause"]
        confirm.DEFAULT_SPECS["pause"] = CommandSpec(original.predicate, 0.1)
        try:
            result = await s.execute("pause")
        finally:
            confirm.DEFAULT_SPECS["pause"] = original
        assert result.outcome == "unconfirmed" and result.state.activity is Activity.CLEANING
    finally:
        await s.stop()


async def test_drop_publishes_offline_then_reconnects(bus, factory):
    s = _session(bus, factory)
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.CONNECTED)
        factory.get(I5).drop(offline=False)
        await _until(lambda: s.connection is ConnectionState.OFFLINE, 1)
        await _until(lambda: s.connection is ConnectionState.CONNECTED)
    finally:
        await s.stop()


async def test_state_changed_is_coalesced(bus, factory):
    s = _session(bus, factory, coalesce_s=0.2)
    events = []

    async def record(event):
        events.append(event)

    bus.subscribe(record, types=[EventType.STATE_CHANGED])
    await s.start()
    try:
        await _until(lambda: s.has_state)
        fake = factory.get(I5)
        for pct in (90, 91, 92, 93):
            fake.emit({"batPct": pct})
        await asyncio.sleep(0.5)
        await bus.drain()
        assert 1 <= len(events) <= 3
        assert events[-1].data["state"]["battery_pct"] == 93
    finally:
        await s.stop()
