import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from roomba_kit.core.models import Activity, Initiator
from roomba_kit.profiles.braava_m6 import PROFILE as M6
from roomba_kit.profiles.roomba_i5 import PROFILE as I5
from roomba_kit.service.normalize import merge, normalize

CAPTURES = Path(__file__).parents[1] / "fixtures" / "captures"
AT = datetime(2026, 11, 8, 12, tzinfo=UTC)
I5_ID, M6_ID = "0" * 31 + "1", "0" * 31 + "2"


def _reported(name):
    return json.loads((CAPTURES / name).read_text())["state"]["reported"]


def _status(phase, cycle, bat=100, **extra):
    return {"batPct": bat, "cleanMissionStatus": {"phase": phase, "cycle": cycle, **extra}}


def test_i5_capture_docked():
    s = normalize(I5_ID, _reported("i5_state.json"), I5, AT)
    assert (s.robot_id, s.at, s.battery_pct, s.activity) == (I5_ID, AT, 100, Activity.DOCKED)
    assert s.mission is None
    assert s.error is None and s.not_ready is None
    assert s.bin is not None and (s.bin.present, s.bin.full) == (True, False)
    assert s.clean_base is not None and (s.clean_base.present, s.clean_base.state) == (True, 301)
    assert s.pad is None and s.tank is None and s.lid_open is None
    assert (s.signal.rssi, s.signal.snr) == (-39, 24)
    assert (s.map.map_id, s.map.version) == ("FbKwAujERSazAALZRD_rEg", "231231T204559")
    assert s.counters.model_dump() == {
        "missions_ok": 264,
        "missions_failed": 186,
        "missions_cancelled": 100,
        "runtime_hours": 680,
        "stuck_count": 172,
    }
    cmd = s.last_command
    assert (cmd.command, cmd.initiator, cmd.room_ids) == ("clean_rooms", Initiator.KIT, ["7"])
    assert (cmd.map_id, cmd.map_version) == ("FbKwAujERSazAALZRD_rEg", "231231T204559")
    assert cmd.at == datetime.fromtimestamp(1794173731, UTC)
    assert len(s.schedules) == 5
    first = s.schedules[0]
    assert (first.enabled, first.days, first.hour, first.minute) == (True, [5], 9, 0)
    assert (first.command, first.team) == ("clean", True)
    assert s.settings == {
        "passes": "one",
        "edge_clean": True,
        "always_finish": False,
        "child_lock": False,
        "eco_charge": False,
        "schedule_hold": False,
    }


def test_m6_capture_docked_with_tank_pad_lid():
    s = normalize(M6_ID, _reported("m6_state.json"), M6, AT)
    assert s.activity is Activity.CHARGING  # batPct 90
    assert s.bin is None and s.clean_base is None
    assert s.pad.detected == "reusableWet"
    assert (s.tank.present, s.tank.level_pct) == (True, 100)
    assert s.lid_open is False
    assert s.counters.runtime_hours is None  # m6 bbrun has no `hr`
    assert (s.last_command.command, s.last_command.room_ids) == ("find", [])
    [sched] = s.schedules
    assert (sched.enabled, sched.days, sched.hour) == (False, [0, 1, 3, 5], 14)
    assert (sched.command, sched.team) == ("clean", False)


@pytest.mark.parametrize(
    ("phase", "cycle", "bat", "expected"),
    [
        ("charge", "none", 100, Activity.DOCKED),
        ("charge", "none", 99, Activity.CHARGING),
        ("run", "clean", 80, Activity.CLEANING),
        ("run", "quick", 80, Activity.CLEANING),
        ("run", "train", 80, Activity.MAPPING),
        ("stop", "clean", 80, Activity.PAUSED),
        ("stop", "none", 80, Activity.UNKNOWN),
        ("hmUsrDock", "clean", 80, Activity.RETURNING),
        ("hmPostMsn", "clean", 80, Activity.RETURNING),
        ("hmMidMsn", "clean", 80, Activity.RETURNING),
        ("evac", "evac", 100, Activity.EMPTYING_BIN),
        ("evac", "none", 100, Activity.EMPTYING_BIN),
        ("charge", "evac", 100, Activity.EMPTYING_BIN),
        ("run", "evac", 100, Activity.EMPTYING_BIN),  # observed on the i5 mid-evac
        ("stuck", "clean", 50, Activity.STUCK),
        ("charge", "clean", 100, Activity.DOCKED),  # a mission waiting on the dock
        ("charge", "clean", 80, Activity.CHARGING),
        ("wobble", "none", 100, Activity.UNKNOWN),
    ],
)
def test_activity_mapping(phase, cycle, bat, expected):
    assert normalize(I5_ID, _status(phase, cycle, bat), I5, AT).activity is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("localApp", Initiator.KIT),
        ("schedule", Initiator.SCHEDULE),
        ("team", Initiator.TEAM),
        ("manual", Initiator.BUTTON),
        ("rmtApp", Initiator.APP),
        ("none", Initiator.OTHER),
        (None, Initiator.OTHER),
    ],
)
def test_mission_initiator(raw, expected):
    s = normalize(I5_ID, _status("run", "clean", initiator=raw, missionId="M1"), I5, AT)
    assert s.mission.initiator is expected and s.mission.id == "M1"


def test_mission_only_while_cycle_active():
    running = _status("run", "clean", missionId="M1", mssnStrtTm=1794163632)
    s = normalize(I5_ID, running, I5, AT)
    assert s.mission.started_at == datetime.fromtimestamp(1794163632, UTC)
    assert (
        normalize(I5_ID, _status("run", "clean", mssnStrtTm=0), I5, AT).mission.started_at is None
    )
    assert normalize(I5_ID, _status("charge", "none", missionId="M1"), I5, AT).mission is None


def test_error_and_not_ready_codes():
    s = normalize(I5_ID, _status("stuck", "clean", error=15, notReady=0), I5, AT)
    assert (s.error.code, s.error.text) == (15, "Robot rebooted") and s.not_ready is None
    s = normalize(I5_ID, _status("charge", "none", error=0, notReady=68), I5, AT)
    assert (
        s.error is None
        and s.not_ready.text == "Not ready yet (transient around docking and bin emptying)"
    )


def test_last_command_with_nulls():
    raw = {"lastCommand": {"command": "addrobotdone", "time": None, "initiator": None}}
    cmd = normalize(I5_ID, raw, I5, AT).last_command
    assert (cmd.command, cmd.at, cmd.initiator, cmd.room_ids) == (
        "addrobotdone",
        None,
        Initiator.OTHER,
        [],
    )


def test_bad_cmdstr_is_tolerated():
    raw = {"cleanSchedule2": [{"enabled": True, "cmdStr": "__import__('os')"}, "junk"]}
    [sched] = normalize(I5_ID, raw, I5, AT).schedules
    assert sched.command is None and sched.days == []


@pytest.mark.parametrize(
    "fragment",
    [
        {},
        {"batPct": 50},
        {"cleanMissionStatus": {"phase": "run"}},
        {"cleanMissionStatus": None, "pmaps": [], "signal": "x", "bin": 1, "dock": None},
        {"lastCommand": {"regions": None}, "bbmssn": [], "cleanSchedule2": {}},
    ],
)
@pytest.mark.parametrize("profile", [I5, M6])
def test_partial_fragments_never_raise(fragment, profile):
    s = normalize(I5_ID, fragment, profile, AT)
    assert s.bin is None and s.pad is None and s.tank is None and s.lid_open is None


def test_merge_is_deep_and_pure():
    base = {"cleanMissionStatus": {"phase": "charge", "cycle": "none"}, "pmaps": [{"a": "1"}]}
    out = merge(base, {"cleanMissionStatus": {"phase": "run"}, "pmaps": [{"b": "2"}]})
    assert out == {"cleanMissionStatus": {"phase": "run", "cycle": "none"}, "pmaps": [{"b": "2"}]}
    assert base["cleanMissionStatus"]["phase"] == "charge"


def test_merge_replays_local_stream():
    state: dict = {}
    for line in (CAPTURES / "m6_local_messages.jsonl").read_text().splitlines():
        msgs = json.loads(line).get("msg")
        for msg in msgs if isinstance(msgs, list) else [msgs]:
            if isinstance(msg, dict) and isinstance(msg.get("state"), dict):
                state = merge(state, msg["state"].get("reported", {}))
                normalize(M6_ID, state, M6, AT)
    final = normalize(M6_ID, state, M6, AT)
    assert final.activity is Activity.CHARGING and final.not_ready.code == 39


def test_a_later_echo_replaces_the_previous_one_whole():
    room_clean = {
        "lastCommand": {
            "command": "start",
            "time": 10,
            "initiator": "localApp",
            "pmap_id": "m",
            "user_pmapv_id": "v",
            "regions": [{"region_id": "7"}],
        }
    }
    find = {"lastCommand": {"command": "find", "time": 11, "initiator": "localApp"}}
    reported = merge(merge({}, room_clean), find)
    echo = normalize("r", reported, I5, datetime.now(UTC)).last_command
    assert (echo.command, echo.room_ids, echo.map_id) == ("find", [], None)


@pytest.mark.parametrize(
    ("raw", "regions", "kit"),
    [
        ("start", [{"region_id": "7"}], "clean_rooms"),
        ("start", None, "clean"),
        ("dock", None, "dock"),
        ("quick", None, "quick"),
    ],
)
def test_echoes_use_kit_command_names(raw, regions, kit):
    cmd = {"command": raw, "time": 1, "initiator": "localApp"}
    if regions:
        cmd["regions"] = regions
    echo = normalize("r", {"lastCommand": cmd}, I5, datetime.now(UTC)).last_command
    assert echo.command == kit
