import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from roomba_kit.core.events import EventType, encode_data
from roomba_kit.core.models import Activity, Initiator, MissionOutcome
from roomba_kit.profiles.base import SettingMapping
from roomba_kit.profiles.braava_m6 import PROFILE as M6
from roomba_kit.profiles.roomba_i5 import PROFILE as I5
from roomba_kit.service.derive import Deriver, outcome
from roomba_kit.service.normalize import merge, normalize

CAPTURES = Path(__file__).parents[1] / "fixtures" / "captures"
I5_ID, M6_ID = "0" * 31 + "1", "0" * 31 + "2"
T0 = datetime(2026, 11, 8, 12, tzinfo=UTC)


class Recorder:
    def __init__(self):
        self.events = []

    async def publish(self, type_, robot_id, data, at=None):
        self.events.append((type_, robot_id, encode_data(type_, data)))

    def types(self):
        return [t for t, _, _ in self.events]

    def of(self, type_):
        return [d for t, _, d in self.events if t is type_]


class Robot:
    """Feeds raw fragments through merge + normalize into a Deriver, one second apart."""

    def __init__(self, deriver, robot_id=I5_ID, profile=I5, reported=None):
        self.deriver, self.robot_id, self.profile = deriver, robot_id, profile
        self.reported = reported or {
            "batPct": 100,
            "bin": {"present": True, "full": False},
            "cleanMissionStatus": {"phase": "charge", "cycle": "none", "missionId": "OLD"},
        }
        self.at = T0

    async def emit(self, fragment=None, **status):
        if status:
            fragment = {**(fragment or {}), "cleanMissionStatus": status}
        self.reported = merge(self.reported, fragment or {})
        self.at += timedelta(seconds=1)
        await self.deriver.update(normalize(self.robot_id, self.reported, self.profile, self.at))


def _bin_pause(reported):
    return None if "binPause" not in reported else not reported["binPause"]


# Settings mappings land with US6; a stand-in mapping exercises `setting.changed`.
I5_WITH_SETTING = dataclasses.replace(
    I5,
    settings={
        "always_finish": SettingMapping(
            read=_bin_pause,
            write=lambda v: {"binPause": not v},
            group=("binPause",),
            confirm=lambda r, v: _bin_pause(r) == v,
        )
    },
)


@pytest.fixture
def bus():
    return Recorder()


@pytest.fixture
def ids():
    return iter(f"kit-{n}" for n in range(100))


@pytest.fixture
def robot(bus, ids):
    return Robot(Deriver(bus, new_id=lambda: next(ids)))


def _echo(command, at=None, **extra):
    t = int((at or T0).timestamp())
    return {"lastCommand": {"command": command, "time": t, "initiator": "localApp", **extra}}


async def test_whole_mission_sequence_and_ids(robot, bus):
    await robot.emit()  # first view: no events
    await robot.emit(_echo("start"))
    await robot.emit(phase="charge", cycle="clean", missionId="OLD")  # transient, stale ID
    assert robot.deriver.current(I5_ID) is None
    await robot.emit(phase="run", cycle="clean", missionId="M1", initiator="localApp")
    current = robot.deriver.current(I5_ID)
    assert current.id == "M1" and current.initiator is Initiator.KIT
    await robot.emit(phase="hmPostMsn", cycle="clean", missionId="M1")
    await robot.emit(phase="charge", cycle="none", missionId="M1")
    assert bus.types() == [
        EventType.MISSION_STARTED,
        EventType.ACTIVITY_CHANGED,
        EventType.ACTIVITY_CHANGED,
        EventType.ACTIVITY_CHANGED,
        EventType.ROBOT_DOCKED,
        EventType.MISSION_FINISHED,
    ]
    changes = [(d["from"], d["to"], d["mission_id"]) for d in bus.of(EventType.ACTIVITY_CHANGED)]
    assert changes == [
        ("docked", "cleaning", "M1"),
        ("cleaning", "returning", "M1"),
        ("returning", "docked", "M1"),
    ]
    assert bus.of(EventType.ROBOT_DOCKED) == [{"mission_id": "M1"}]
    (done,) = bus.of(EventType.MISSION_FINISHED)
    assert done["mission"]["outcome"] == "completed" and done["mission"]["duration_s"] == 3
    assert robot.deriver.current(I5_ID) is None
    assert robot.deriver.find(I5_ID, "M1").outcome is MissionOutcome.COMPLETED

    # The next mission reuses the robot's ID (as the fake does): it gets a kit ID instead.
    await robot.emit(phase="run", cycle="clean", missionId="M1")
    assert robot.deriver.current(I5_ID).id == "kit-0"


async def test_cycle_that_never_runs_is_not_a_mission(robot, bus):
    await robot.emit()
    await robot.emit(phase="charge", cycle="evac")
    await robot.emit(phase="evac", cycle="evac")
    await robot.emit(phase="charge", cycle="none")
    assert EventType.MISSION_STARTED not in bus.types()
    assert EventType.MISSION_FINISHED not in bus.types()


async def test_rooms_from_a_fresh_room_clean_echo(robot, bus):
    await robot.emit(_echo("start", at=T0 - timedelta(hours=3), regions=[{"region_id": "9"}]))
    await robot.emit(phase="run", cycle="clean", missionId="A", mssnStrtTm=int(T0.timestamp()))
    assert robot.deriver.current(I5_ID).rooms_requested == []  # stale echo: whole home
    await robot.emit(phase="charge", cycle="none")
    regions = [{"region_id": "7", "type": "rid"}, {"region_id": "11", "type": "rid"}]
    await robot.emit(_echo("start", at=T0, regions=regions))
    await robot.emit(phase="run", cycle="clean", missionId="B", mssnStrtTm=int(T0.timestamp()))
    started = bus.of(EventType.MISSION_STARTED)[-1]["mission"]
    assert started["rooms_requested"] == ["7", "11"]
    assert started["started_at"] == "2026-11-08T12:00:00Z"


@pytest.mark.parametrize(
    ("last", "end", "command", "error", "expected"),
    [
        (Activity.PAUSED, Activity.CHARGING, "pause", False, MissionOutcome.MANUALLY_DOCKED),
        (Activity.STUCK, Activity.DOCKED, None, True, MissionOutcome.MANUALLY_DOCKED),
        (Activity.PAUSED, Activity.UNKNOWN, "stop", False, MissionOutcome.CANCELLED),
        (Activity.RETURNING, Activity.DOCKED, "dock", False, MissionOutcome.CANCELLED),
        (Activity.STUCK, Activity.STUCK, None, True, MissionOutcome.STUCK),
        (Activity.CLEANING, Activity.UNKNOWN, None, True, MissionOutcome.FAILED),
        (Activity.PAUSED, Activity.UNKNOWN, None, False, MissionOutcome.CANCELLED),
        (Activity.RETURNING, Activity.CHARGING, "start", False, MissionOutcome.COMPLETED),
    ],
)
def test_outcome_rules(last, end, command, error, expected):
    assert outcome(last, end, command, error) is expected


async def test_stuck_error_bin_and_settings(bus, ids):
    robot = Robot(Deriver(bus, new_id=lambda: next(ids)), profile=I5_WITH_SETTING)
    await robot.emit({"binPause": True})
    await robot.emit(phase="run", cycle="clean", missionId="S")
    await robot.emit(phase="stuck", cycle="clean", missionId="S", error=17)
    await robot.emit({"bin": {"full": True}, "binPause": False})
    await robot.emit({"bin": {"full": True}})  # still full: once
    await robot.emit(phase="stuck", cycle="none", missionId="S", error=17)
    e17 = robot.deriver.find(I5_ID, "S").errors[0].model_dump()  # code + its readable text
    assert e17["code"] == 17 and e17["text"]
    assert bus.of(EventType.ROBOT_STUCK) == [{"error": e17}]
    assert bus.of(EventType.ERROR_RAISED) == [{"kind": "error", **e17}]
    assert len(bus.of(EventType.BIN_FULL)) == 1
    assert bus.of(EventType.SETTING_CHANGED) == [
        {"setting": "always_finish", "from": False, "to": True}
    ]
    (done,) = bus.of(EventType.MISSION_FINISHED)
    assert done["mission"]["outcome"] == "stuck"
    assert done["mission"]["errors"] == [e17]
    await robot.emit(phase="charge", cycle="none", error=0, notReady=68)
    assert bus.of(EventType.ERROR_CLEARED) == [{"kind": "error", **e17}]
    last = bus.of(EventType.ERROR_RAISED)[-1]
    assert (last["kind"], last["code"]) == ("not_ready", 68) and last["text"]


async def test_m6_capture_replay(bus, ids):
    """The real m6 start → pause → resume → pause → dock stream from hardware."""
    robot = Robot(Deriver(bus, new_id=lambda: next(ids)), M6_ID, M6, reported={})
    for line in (CAPTURES / "m6_local_messages.jsonl").read_text().splitlines():
        for msg in json.loads(line)["msg"]:
            await robot.emit(msg.get("state", {}).get("reported", {}))
    (started,) = bus.of(EventType.MISSION_STARTED)
    (finished,) = bus.of(EventType.MISSION_FINISHED)
    # The first fragment of the cycle still carried the previous mission's ID.
    assert started["mission"]["id"] == finished["mission"]["id"] == "01M6Y7RSW0T3M943E9K42Q03P1"
    assert finished["mission"]["outcome"] == "cancelled"  # docked by command mid-mission
    tos = [d["to"] for d in bus.of(EventType.ACTIVITY_CHANGED)]
    assert tos[:4] == ["cleaning", "paused", "cleaning", "paused"]
    assert "returning" in tos and tos[-1] in ("docked", "charging")
    assert bus.types().index(EventType.ROBOT_DOCKED) < bus.types().index(EventType.MISSION_FINISHED)


async def test_a_new_robot_mission_id_mid_mission_is_a_new_mission(robot, bus):
    """The robot finished M1 and started M2 while the link was down: two missions, not one."""
    await robot.emit()
    await robot.emit(phase="run", cycle="clean", missionId="M1")
    await robot.emit(phase="run", cycle="clean", missionId="")  # decision #11: not a new one
    assert robot.deriver.current(I5_ID).id == "M1"
    await robot.emit(_echo("stop"))
    await robot.emit(phase="run", cycle="clean", missionId="M2")  # first state after reconnect
    missions = [(t, d["mission"]) for t, _, d in bus.events if "mission" in d]
    assert [(t, m["id"]) for t, m in missions] == [
        (EventType.MISSION_STARTED, "M1"),
        (EventType.MISSION_FINISHED, "M1"),
        (EventType.MISSION_STARTED, "M2"),
    ]
    assert missions[1][1]["outcome"] == "unknown" and missions[1][1]["ended_at"] is None
    assert robot.deriver.current(I5_ID).id == "M2"
    await robot.emit(phase="charge", cycle="none", missionId="M2")
    (_, m1), (_, m2) = [
        (t, d["mission"]) for t, _, d in bus.events if t is EventType.MISSION_FINISHED
    ]
    assert m2["id"] == "M2" and m2["outcome"] == "completed"  # M1's `stop` isn't M2's


async def test_a_mission_that_started_without_an_id_adopts_the_first_one(robot, bus):
    await robot.emit()
    await robot.emit(phase="run", cycle="clean", missionId="")
    await robot.emit(phase="run", cycle="clean", missionId="M1")
    await robot.emit(phase="run", cycle="clean", missionId="M1")
    assert bus.types().count(EventType.MISSION_STARTED) == 1
    assert EventType.MISSION_FINISHED not in bus.types()


async def test_driving_home_after_a_cancel_is_not_a_mission(robot, bus):
    """HW-1: an i5 sent `dock` after a cancelled clean drove home with an open `dock` cycle,
    evacuated and flipped returning/docked on arrival; that was logged as two missions."""
    await robot.emit()
    await robot.emit(phase="stop", cycle="none", missionId="M1")
    await robot.emit(phase="hmUsrDock", cycle="dock", missionId="M1")
    await robot.emit(phase="evac", cycle="evac", missionId="M1")
    await robot.emit(phase="hmUsrDock", cycle="dock", missionId="M1")
    await robot.emit(phase="charge", cycle="none", missionId="M1")
    assert EventType.MISSION_STARTED not in bus.types()
    assert EventType.MISSION_FINISHED not in bus.types()
