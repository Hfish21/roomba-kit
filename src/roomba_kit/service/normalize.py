"""Map a merged raw `state.reported` dict to a kit `RobotState` (research R8).

Protocol keys stay in this module and in profiles. Fragments are partial right after connect,
so every read tolerates a missing or oddly typed key.
"""

import ast
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from roomba_kit.core.capabilities import COMMAND_CAPABILITIES, Capability
from roomba_kit.core.models import (
    Activity,
    BinState,
    CleanBaseState,
    CodeText,
    CommandEcho,
    Counters,
    Initiator,
    MapRef,
    MissionRef,
    PadState,
    RobotSchedule,
    RobotState,
    Signal,
    TankState,
)
from roomba_kit.profiles.base import ModelProfile, Reported
from roomba_kit.profiles.texts import ERROR_TEXTS, NOT_READY_TEXTS

_INITIATORS: dict[str, Initiator] = {
    "localApp": Initiator.KIT,
    "schedule": Initiator.SCHEDULE,
    "team": Initiator.TEAM,
    "manual": Initiator.BUTTON,
    "rmtApp": Initiator.APP,
}
_RETURNING_PHASES = frozenset({"hmUsrDock", "hmPostMsn", "hmMidMsn"})


_REPLACED_WHOLE = frozenset({"lastCommand"})
"""Keys the robot always reports as a complete object. Deep-merging them would let a later
`find` echo inherit an earlier room clean's `regions` and `pmap_id`."""


def merge(base: Mapping[str, Any], fragment: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge a `state.reported` fragment into `base`. Dicts merge; anything else replaces.

    Top-level keys in `_REPLACED_WHOLE` always replace. Neither input is mutated.
    """
    out = dict(base)
    for key, value in fragment.items():
        prior = out.get(key)
        if key in _REPLACED_WHOLE:
            out[key] = value
        elif isinstance(value, Mapping) and isinstance(prior, Mapping):
            out[key] = merge(prior, value)
        else:
            out[key] = value
    return out


def normalize(robot_id: str, reported: Reported, profile: ModelProfile, at: datetime) -> RobotState:
    status = _map(reported.get("cleanMissionStatus"))
    phase, cycle = _str(status.get("phase")), _str(status.get("cycle"))
    battery = _int(reported.get("batPct"))
    bbmssn, bbrun = _map(reported.get("bbmssn")), _map(reported.get("bbrun"))
    return RobotState(
        robot_id=robot_id,
        at=at,
        battery_pct=battery,
        activity=activity(phase, cycle, battery),
        mission=_mission(status, cycle),
        last_command=_last_command(reported.get("lastCommand"), profile),
        error=_code(_int(status.get("error")), profile.error_texts, ERROR_TEXTS, "Error"),
        not_ready=_code(
            _int(status.get("notReady")), profile.not_ready_texts, NOT_READY_TEXTS, "Not ready"
        ),
        signal=_signal(reported.get("signal")),
        map=_map_ref(reported.get("pmaps")),
        schedules=_schedules(reported.get("cleanSchedule2"), profile),
        counters=Counters(
            missions_ok=_int(bbmssn.get("nMssnOk")),
            missions_failed=_int(bbmssn.get("nMssnF")),
            missions_cancelled=_int(bbmssn.get("nMssnC")),
            runtime_hours=_int(bbrun.get("hr")),
            stuck_count=_int(bbrun.get("nStuck")),
        ),
        bin=_bin(reported) if profile.supports(Capability.STATE_BIN) else None,
        clean_base=_clean_base(reported) if profile.supports(Capability.STATE_CLEAN_BASE) else None,
        pad=_pad(reported) if profile.supports(Capability.STATE_PAD) else None,
        tank=_tank(reported) if profile.supports(Capability.STATE_TANK) else None,
        lid_open=_bool(reported.get("lidOpen")) if profile.supports(Capability.STATE_LID) else None,
        settings={name: m.read(reported) for name, m in profile.settings.items()},
    )


def activity(phase: str | None, cycle: str | None, battery_pct: int | None) -> Activity:
    """Research R8. `evac` wins over `run`: the i5 reports `run`/`evac` while emptying."""
    if phase == "evac" or cycle == "evac":
        return Activity.EMPTYING_BIN
    if phase == "charge" and cycle == "none":
        charging = battery_pct is not None and battery_pct < 100
        return Activity.CHARGING if charging else Activity.DOCKED
    if phase == "run":
        return Activity.MAPPING if cycle == "train" else Activity.CLEANING
    if phase == "stop" and cycle is not None and cycle != "none":
        return Activity.PAUSED
    if phase in _RETURNING_PHASES:
        return Activity.RETURNING
    if phase == "stuck":
        return Activity.STUCK
    return Activity.UNKNOWN


def initiator(raw: object) -> Initiator:
    return _INITIATORS.get(raw, Initiator.OTHER) if isinstance(raw, str) else Initiator.OTHER


def code_text(
    code: int, overrides: Mapping[int, str], shared: Mapping[int, str], label: str
) -> str:
    """Readable text for an error or not-ready code (T055): the profile's override, then the
    shared table in `profiles/texts.py`, else `"<label> <code>"`."""
    return overrides.get(code) or shared.get(code) or f"{label} {code}"


def _code(
    code: int | None, overrides: Mapping[int, str], shared: Mapping[int, str], label: str
) -> CodeText | None:
    if not code:
        return None
    return CodeText(code=code, text=code_text(code, overrides, shared, label))


def _mission(status: Mapping[str, Any], cycle: str | None) -> MissionRef | None:
    if cycle is None or cycle == "none":
        return None
    return MissionRef(
        id=_str(status.get("missionId")) or "",
        started_at=_epoch(status.get("mssnStrtTm")),
        initiator=initiator(status.get("initiator")),
    )


def kit_command(raw: str, room_ids: list[str], profile: ModelProfile) -> str:
    """The kit command name for a protocol command echo (`start` → `clean` / `clean_rooms`).

    Commands the kit doesn't define (another client's) pass through unchanged.
    """
    names = [c.value for c in COMMAND_CAPABILITIES if profile.protocol_command(c.value) == raw]
    if not names:
        return raw
    if "clean_rooms" in names and room_ids:
        return "clean_rooms"
    if "clean" in names:
        return "clean"
    return sorted(names)[0]


def _last_command(raw: object, profile: ModelProfile) -> CommandEcho | None:
    cmd = _map(raw)
    command = _str(cmd.get("command"))
    if command is None:
        return None
    regions = cmd.get("regions")
    room_ids = [
        str(r["region_id"])
        for r in (regions if isinstance(regions, list) else [])
        if isinstance(r, Mapping) and r.get("region_id") is not None
    ]
    return CommandEcho(
        command=kit_command(command, room_ids, profile),
        at=_epoch(cmd.get("time")),
        initiator=initiator(cmd.get("initiator")),
        room_ids=room_ids,
        map_id=_str(cmd.get("pmap_id")),
        map_version=_str(cmd.get("user_pmapv_id")),
    )


def _signal(raw: object) -> Signal | None:
    if not isinstance(raw, Mapping):
        return None
    return Signal(rssi=_int(raw.get("rssi")), snr=_int(raw.get("snr")))


def _map_ref(raw: object) -> MapRef | None:
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], Mapping) or not raw[0]:
        return None
    map_id, version = next(iter(raw[0].items()))
    return MapRef(map_id=str(map_id), version=str(version))


def _schedules(raw: object, profile: ModelProfile) -> list[RobotSchedule]:
    if not isinstance(raw, list):
        return []
    out: list[RobotSchedule] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        start = _map(entry.get("start"))
        days = start.get("day")
        cmd = _schedule_cmd(entry)
        params = _map(cmd.get("params"))
        out.append(
            RobotSchedule(
                enabled=entry.get("enabled") is True,
                days=[d for d in days if _int(d) is not None] if isinstance(days, list) else [],
                hour=_int(start.get("hour")) or 0,
                minute=_int(start.get("min")) or 0,
                command=kit_command(c, [], profile) if (c := _str(cmd.get("command"))) else None,
                team="team" in params,
            )
        )
    return out


def _schedule_cmd(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    """The schedule's command: a `cmd` dict (i5) or a Python-literal `cmdStr` string (m6)."""
    if isinstance(entry.get("cmd"), Mapping):
        return _map(entry["cmd"])
    text = entry.get("cmdStr")
    if not isinstance(text, str):
        return {}
    try:
        return _map(ast.literal_eval(text))
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return {}


def _bin(reported: Reported) -> BinState | None:
    raw = reported.get("bin")
    if not isinstance(raw, Mapping):
        return None
    return BinState(present=_bool(raw.get("present")), full=_bool(raw.get("full")))


def _clean_base(reported: Reported) -> CleanBaseState | None:
    """A Clean Base reports a numeric `dock.state`; a plain Home Base reports only `known`."""
    raw = reported.get("dock")
    if not isinstance(raw, Mapping):
        return None
    state = _int(raw.get("state"))
    return CleanBaseState(present=state is not None, state=state)


def _pad(reported: Reported) -> PadState | None:
    if "detectedPad" not in reported:
        return None
    return PadState(detected=_str(reported.get("detectedPad")))


def _tank(reported: Reported) -> TankState | None:
    if "tankPresent" not in reported and "tankLvl" not in reported:
        return None
    return TankState(
        present=_bool(reported.get("tankPresent")), level_pct=_int(reported.get("tankLvl"))
    )


def _map(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _epoch(value: object) -> datetime | None:
    seconds = _int(value)
    return datetime.fromtimestamp(seconds, UTC) if seconds and seconds > 0 else None
