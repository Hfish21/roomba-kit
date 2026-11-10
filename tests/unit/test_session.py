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
        await _until(lambda: seen and seen[-1] == "connected")
        assert "held_elsewhere" in seen
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
        await _until(lambda: events and events[-1].data["state"]["battery_pct"] == 93, 5)
        assert 1 <= len(events) <= 3
    finally:
        await s.stop()


# -- regressions from the PR #15 review ------------------------------------------------------


async def test_caller_timeout_does_not_kill_the_command_worker(bus, factory):
    factory.get(I5).reaction_delay = 0.2
    s = _session(bus, factory)
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.CONNECTED and s.has_state)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(s.execute("clean"), 0.05)
        result = await asyncio.wait_for(s.execute("find"), 2)
        assert result.outcome == "confirmed"
    finally:
        await s.stop()


async def test_stop_fails_in_flight_and_later_jobs_with_kit_errors(bus, factory):
    factory.get(I5).reaction_delay = 5
    s = _session(bus, factory)
    await s.start()
    await _until(lambda: s.connection is ConnectionState.CONNECTED and s.has_state)
    in_flight = asyncio.create_task(s.execute("clean"))
    await _until(lambda: factory.get(I5).sent_commands)
    await s.stop()
    with pytest.raises(KitError) as exc:
        await in_flight
    assert exc.value.code is ErrorCode.ROBOT_OFFLINE
    with pytest.raises(KitError):
        await asyncio.wait_for(s.execute("find"), 1)


async def test_state_change_during_a_publish_is_not_lost(bus, factory, monkeypatch):
    s = _session(bus, factory, coalesce_s=0.01)
    published = []
    real_publish = bus.publish

    async def slow_publish(type_, robot_id, data, at=None):
        if type_ is EventType.STATE_CHANGED:
            await asyncio.sleep(0.05)
            published.append(data.state.battery_pct)
        return await real_publish(type_, robot_id, data, at)

    monkeypatch.setattr(bus, "publish", slow_publish)
    await s.start()
    try:
        await _until(lambda: s.has_state)
        fake = factory.get(I5)
        fake.emit({"batPct": 41})
        await asyncio.sleep(0.02)  # first flush is mid-publish
        fake.emit({"batPct": 42})
        await _until(lambda: published and published[-1] == 42)
    finally:
        await s.stop()


async def test_credentials_change_while_connecting_reconnects_with_new_password(bus, factory):
    password = {"value": "old"}
    entry = RobotEntry(id=I5, name="Vacuum", address="192.0.2.10", profile="roomba_i5")
    s = RobotSession(
        entry,
        get_profile("roomba_i5"),
        factory,
        lambda: password["value"],
        bus,
        backoff_min=0.01,
        backoff_max=0.02,
    )
    real_set = s._set_connection

    async def change_during_connecting(to, reason=None):
        await real_set(to, reason)
        if to is ConnectionState.CONNECTING and password["value"] == "old":
            password["value"] = "new"
            s.credentials_changed()

    s._set_connection = change_during_connecting
    await s.start()
    try:
        await _until(
            lambda: factory.get(I5).password == "new" and s.connection is ConnectionState.CONNECTED
        )
    finally:
        await s.stop()


async def test_unknown_command_is_invalid_request(bus, factory):
    s = _session(bus, factory)
    with pytest.raises(KitError) as exc:
        await s.execute("state.bin")
    assert exc.value.code is ErrorCode.INVALID_REQUEST


async def test_a_connection_that_sends_no_state_is_retried(bus, factory):
    """HW-1: right after HOME-button pairing an i5 accepted the session but sent no state."""
    fake = factory.get(I5)
    full = dict(fake.reported)
    fake.reported = {}  # connects, but the first state burst is empty
    s = _session(bus, factory, first_state_timeout_s=0.1)
    await s.start()
    try:
        await _until(lambda: fake.connect_calls >= 1 and s.connection is ConnectionState.OFFLINE)
        fake.reported = full
        await _until(lambda: s.has_state and s.connection is ConnectionState.CONNECTED)
        assert fake.connect_calls >= 2
    finally:
        await s.stop()


async def test_a_link_silent_mid_mission_is_reconnected(bus, factory):
    """HW-1: an i5's link stayed up but carried no state for 30 min of cleaning."""
    fake = factory.get(I5)
    s = _session(bus, factory, mission_silence_s=0.2)
    await s.start()
    try:
        await _until(lambda: s.has_state)
        await s.execute("clean")
        calls = fake.connect_calls
        await _until(lambda: fake.connect_calls > calls, timeout=3)  # silence → reconnect
        await _until(lambda: s.connection is ConnectionState.CONNECTED)
    finally:
        await s.stop()


async def test_a_quiet_docked_robot_is_left_alone(bus, factory):
    fake = factory.get(I5)
    s = _session(bus, factory, mission_silence_s=0.1)  # idle limit stays at its 10 min default
    await s.start()
    try:
        await _until(lambda: s.has_state and s.connection is ConnectionState.CONNECTED)
        calls = fake.connect_calls
        await asyncio.sleep(0.5)
        assert fake.connect_calls == calls
    finally:
        await s.stop()


async def test_reboot_is_sent_as_reset(bus, factory):
    """HW-1: the i5 ignored `reboot`; the firmware command is `reset`."""
    s = _session(bus, factory)
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.CONNECTED and s.has_state)
        from roomba_kit.profiles.base import CommandSpec
        from roomba_kit.service import confirm

        original = confirm.DEFAULT_SPECS["reboot"]
        confirm.DEFAULT_SPECS["reboot"] = CommandSpec(original.predicate, 0.05)
        try:
            await s.execute("reboot")
        finally:
            confirm.DEFAULT_SPECS["reboot"] = original
        assert factory.get(I5).sent_commands[-1][0] == "reset"
    finally:
        await s.stop()


async def test_i5_dock_while_cleaning_pauses_first(bus, factory):
    """HW-1: an i5 echoed `dock` sent while cleaning but kept cleaning; from paused it docked."""
    s = _session(bus, factory)
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.CONNECTED and s.has_state)
        await s.execute("clean")
        result = await s.execute("dock")
        assert result.outcome == "confirmed"
        assert [c for c, _ in factory.get(I5).sent_commands] == ["start", "pause", "dock"]
    finally:
        await s.stop()


async def test_m6_dock_while_cleaning_pauses_first_too(bus, factory):
    """HW-1: the m6 also kept mopping after an echoed `dock`; it needs a pause first."""
    s = _session(bus, factory, robot_id=M6, profile="braava_m6")
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.CONNECTED and s.has_state)
        await s.execute("clean")
        await s.execute("dock")
        assert [c for c, _ in factory.get(M6).sent_commands] == ["start", "pause", "dock"]
    finally:
        await s.stop()


async def test_a_command_with_no_reaction_at_all_reconnects(bus, factory):
    """HW-1: an i5 link stayed "connected" but delivered nothing; commands vanished."""
    from roomba_kit.profiles.base import CommandSpec
    from roomba_kit.service import confirm

    fake = factory.get(I5)
    s = _session(bus, factory)
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.CONNECTED and s.has_state)
        fake.drop_all_commands = True  # sent, but no echo and no state change
        calls = fake.connect_calls
        original = confirm.DEFAULT_SPECS["find"]
        confirm.DEFAULT_SPECS["find"] = CommandSpec(original.predicate, 0.1)
        try:
            heard = s._fragment_count
            assert (await s.execute("find")).outcome == "unconfirmed"
        finally:
            confirm.DEFAULT_SPECS["find"] = original
        # A real reconnect: the transport was torn down and the robot re-sent its state.
        # (Regression: the kit declared the link dead but reused the open transport.)
        await _until(lambda: fake.connect_calls > calls and s._fragment_count > heard)
    finally:
        await s.stop()


async def test_a_link_silent_while_docked_is_reconnected(bus, factory):
    """HW-1: a docked m6's link stayed "connected" 70 min while delivering nothing."""
    fake = factory.get(I5)
    s = _session(bus, factory, idle_silence_s=0.2, mission_silence_s=0.2)
    await s.start()
    try:
        await _until(lambda: s.has_state)
        heard = s._fragment_count
        await _until(lambda: s._fragment_count > heard, timeout=3)  # reconnect re-sends state
        assert fake.connected
    finally:
        await s.stop()


async def test_dock_after_stop_is_retried_until_the_robot_heads_home(bus, factory, monkeypatch):
    """HW-1: after `stop`, both models ignored `dock` for 30-70 s, then accepted it."""
    from roomba_kit.service import session as session_module

    monkeypatch.setattr(session_module, "DOCK_RETRY_S", 0.01)
    fake = factory.get(I5)
    real_send = fake.send_command
    docks = []

    async def send(name, params=None):
        if name == "dock":
            docks.append(name)
            if len(docks) < 3:  # ignored (but echoed) twice, then accepted
                fake.sent_commands.append((name, params))
                fake.emit(
                    {
                        "lastCommand": {
                            "command": "dock",
                            "time": len(docks),
                            "initiator": "localApp",
                        }
                    }
                )
                return
        await real_send(name, params)

    monkeypatch.setattr(fake, "send_command", send)
    s = _session(bus, factory)
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.CONNECTED and s.has_state)
        await s.execute("clean")
        await s.execute("stop")
        from roomba_kit.profiles.base import CommandSpec
        from roomba_kit.service import confirm

        original = confirm.DEFAULT_SPECS["dock"]
        confirm.DEFAULT_SPECS["dock"] = CommandSpec(original.predicate, 0.05)
        try:
            result = await s.execute("dock")
        finally:
            confirm.DEFAULT_SPECS["dock"] = original
        assert result.outcome == "confirmed" and len(docks) == 3
    finally:
        await s.stop()


def test_m6_uses_longer_confirmation_windows():
    from roomba_kit.profiles.loader import get_profile
    from roomba_kit.service.confirm import spec_for

    m6, i5 = get_profile("braava_m6"), get_profile("roomba_i5")
    assert spec_for("stop", m6).window_s == 30 and spec_for("stop", i5).window_s == 10
    assert spec_for("find", m6).window_s == 5  # untouched commands keep the default
    assert spec_for("clean_rooms", m6).window_s == 90


async def test_same_command_twice_in_one_second_confirms_by_echo_message(bus, factory):
    """Echo times are whole seconds, so a second `clean` in the same second as the first
    can't be told apart by timestamp; the echo message itself proves it was received."""
    s = _session(bus, factory)
    await s.start()
    try:
        await _until(lambda: s.connection is ConnectionState.CONNECTED and s.has_state)
        assert (await s.execute("clean")).outcome == "confirmed"
        again = await asyncio.wait_for(s.execute("clean"), 5)
        assert again.outcome == "confirmed" and again.waited_ms < 2000
    finally:
        await s.stop()
