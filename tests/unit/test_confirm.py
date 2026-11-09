from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from roomba_kit.core.models import Activity, CommandEcho, Initiator, MissionRef, RobotState
from roomba_kit.profiles.base import CommandSpec
from roomba_kit.profiles.braava_m6 import PROFILE as M6
from roomba_kit.service.confirm import DEFAULT_SPECS, await_confirmation, spec_for

SENT = datetime(2026, 11, 8, 12, tzinfo=UTC)
MISSION = MissionRef(id="M1", initiator=Initiator.KIT)


def _state(activity, **kw):
    return RobotState(robot_id="0" * 31 + "2", at=SENT, activity=activity, **kw)


class FakeSource:
    """Scripted changes `(after_s, state | "reconnect")` on a virtual clock; nothing sleeps."""

    def __init__(self, state, script=()):
        self.state, self.script, self.now = state, list(script), 0.0
        self.change_seq, self.reconnected_at = 0, None

    def clock(self):
        return self.now

    def reconnected_since(self, at):
        return self.reconnected_at is not None and self.reconnected_at > at

    async def wait_change(self, after_seq, timeout_s):
        assert after_seq == self.change_seq
        if not self.script or self.script[0][0] - self.now > timeout_s:
            self.now += timeout_s
            return
        self.now, change = self.script.pop(0)
        if change == "reconnect":
            self.reconnected_at = SENT + timedelta(seconds=self.now)
        else:
            self.state = change
        self.change_seq += 1


async def _confirm(source, command, before=None, spec=None):
    spec = spec or DEFAULT_SPECS[command]
    return await await_confirmation(
        source, command, spec, before=before, sent_at=SENT, clock=source.clock
    )


async def test_pause_landing_5s_late_is_confirmed():
    running = _state(Activity.CLEANING, mission=MISSION)
    src = FakeSource(running, [(1.0, running), (5.0, _state(Activity.PAUSED, mission=MISSION))])
    result = await _confirm(src, "pause", before=running)
    assert result.outcome == "confirmed" and result.waited_ms == 5000
    assert result.state.activity is Activity.PAUSED and result.state.mission == MISSION


async def test_pause_that_never_lands_is_unconfirmed_with_state():
    running = _state(Activity.CLEANING, mission=MISSION)
    src = FakeSource(running, [(3.0, running), (11.0, _state(Activity.PAUSED))])
    result = await _confirm(src, "pause", before=running)
    assert result.outcome == "unconfirmed" and result.waited_ms == 10000
    assert result.state.activity is Activity.CLEANING and result.command == "pause"


def _echo(command, seconds):
    return CommandEcho(command=command, at=SENT + timedelta(seconds=seconds), initiator="kit")


async def test_find_needs_a_newer_echo():
    before = _state(Activity.DOCKED, last_command=_echo("find", -60))
    stale = FakeSource(before, [(1.0, before)])
    assert (await _confirm(stale, "find", before=before)).outcome == "unconfirmed"

    fresh = FakeSource(before, [(0.8, _state(Activity.DOCKED, last_command=_echo("find", 1)))])
    result = await _confirm(fresh, "find", before=before)
    assert result.outcome == "confirmed" and result.waited_ms == 800


async def test_find_with_no_prior_echo():
    docked = _state(Activity.DOCKED)
    src = FakeSource(docked, [(1.0, _state(Activity.DOCKED, last_command=_echo("find", 1)))])
    assert (await _confirm(src, "find", before=docked)).outcome == "confirmed"


@pytest.mark.parametrize(
    ("command", "target"),
    [
        ("clean", Activity.CLEANING),
        ("clean_rooms", Activity.CLEANING),
        ("resume", Activity.CLEANING),
        ("dock", Activity.RETURNING),
        ("evac", Activity.EMPTYING_BIN),
        ("train", Activity.MAPPING),
    ],
)
async def test_activity_predicates(command, target):
    src = FakeSource(_state(Activity.STUCK), [(2.0, _state(target))])
    assert (await _confirm(src, command)).outcome == "confirmed"


async def test_stop_confirms_when_mission_ends():
    src = FakeSource(_state(Activity.PAUSED, mission=MISSION), [(2.0, _state(Activity.UNKNOWN))])
    assert (await _confirm(src, "stop")).outcome == "confirmed"


async def test_reboot_confirms_on_reconnect():
    src = FakeSource(_state(Activity.DOCKED), [(90.0, "reconnect")])
    result = await _confirm(src, "reboot")
    assert result.outcome == "confirmed" and result.waited_ms == 90000


async def test_already_true_confirms_without_waiting():
    src = FakeSource(_state(Activity.DOCKED))
    result = await _confirm(src, "dock")
    assert result.outcome == "confirmed" and result.waited_ms == 0


def test_profile_override_wins():
    override = replace(DEFAULT_SPECS["pause"], window_s=30)
    profile = replace(M6, command_overrides={"pause": override})
    assert spec_for("pause", profile) is override
    assert spec_for("resume", profile) is DEFAULT_SPECS["resume"]
    with pytest.raises(KeyError):
        spec_for("dance", profile)


async def test_override_window_is_used():
    running = _state(Activity.CLEANING)
    late = FakeSource(running, [(25.0, _state(Activity.PAUSED))])
    result = await _confirm(late, "pause", spec=CommandSpec(DEFAULT_SPECS["pause"].predicate, 30))
    assert result.outcome == "confirmed" and result.waited_ms == 25000


def test_default_windows_match_r9():
    windows = {name: spec.window_s for name, spec in DEFAULT_SPECS.items()}
    assert windows == {
        "clean": 20,
        "clean_rooms": 20,
        "pause": 10,
        "resume": 10,
        "stop": 10,
        "dock": 10,
        "find": 5,
        "evac": 15,
        "reboot": 180,
        "train": 20,
    }
