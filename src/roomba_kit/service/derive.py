"""Derived events from normalized state (contracts/events.md).

A service component registers one state listener and publishes `activity.changed`,
`mission.started`/`mission.finished`, `robot.docked`, `robot.stuck`, `bin.full`,
`error.raised`/`error.cleared` and `setting.changed`. It also keeps each robot's current mission
in memory for `mission_wait` and history (`current_mission`). Missions aren't persisted here:
`mission.started` and `mission.finished` each carry the whole `Mission`, enough to persist from.

Mission rules (captures in tests/fixtures/captures):
- A mission opens at cycle none→X but is only *started* once the robot is visibly running
  (cleaning, mapping, paused, returning or stuck). On hardware the first fragment of a new cycle
  still carries the previous mission's ID and the robot sits in a `charge` transient; the new ID
  arrives with `run`. A cycle that never runs (a dock-side `evac`, a start that fails) is not a
  mission and publishes nothing.
- Its ID is the robot's mission ID, or a kit UUID when the robot gave none or reused one.
- Outcome, first match: ended on the dock straight from paused/stuck → `manually_docked` (it was
  placed there); last command echoed during the mission was `stop` or `dock` → `cancelled`;
  last activity stuck → `stuck`; an error code at the end → `failed`; ended off the dock →
  `cancelled`; else `completed`. These are inferred from local state; the cloud's `done` field
  isn't available locally.
- A different non-empty robot mission ID during a started mission means the robot moved on while
  the kit wasn't watching (e.g. a link outage): the old mission finishes as `unknown` and the new
  one opens under the usual start rule. An empty ID mid-cycle is not a change (decision #11).
- After a restart, `restore` re-attaches: history hands back each robot's recent missions, and an
  `in_progress` one continues (no second `mission.started`) once the robot is seen running under
  that robot mission ID. If the robot has moved on, the stale record finishes as `unknown`.
"""

import asyncio
import logging
import uuid
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from roomba_kit.core.events import (
    ActivityChangedData,
    BinFullData,
    ErrorEventData,
    EventType,
    MissionEventData,
    RobotDockedData,
    RobotStuckData,
    SettingChangedData,
)
from roomba_kit.core.models import (
    Activity,
    CodeText,
    CommandEcho,
    Mission,
    MissionOutcome,
    RobotState,
)
from roomba_kit.service.components import Stop, component
from roomba_kit.service.eventbus import EventBus

if TYPE_CHECKING:
    from roomba_kit.service.app import ServiceContext

log = logging.getLogger(__name__)

ON_DOCK = frozenset({Activity.DOCKED, Activity.CHARGING})
RUNNING = frozenset(
    {Activity.CLEANING, Activity.MAPPING, Activity.PAUSED, Activity.RETURNING, Activity.STUCK}
)
STARTS = frozenset({Activity.CLEANING, Activity.MAPPING})
"""A new mission needs actual cleaning or mapping. HW-1: an i5 driving home after a
cancelled mission (and an evac flip on arrival) was logged as two extra missions."""
CANCEL_COMMANDS = frozenset({"stop", "dock"})
ECHO_SLACK = timedelta(minutes=2)
"""A room-clean echo counts for a mission when it's at most this much older than the start."""
RECENT_MISSIONS = 20


@dataclass
class _Track:
    """One cycle from none→X to X→none. `mission` is None until the robot is seen running."""

    opened_at: datetime
    robot_ref: str
    mission: Mission | None = None
    last_activity: Activity | None = None
    last_command: str | None = None
    errors: list[CodeText] = field(default_factory=list)


@dataclass
class _Robot:
    last: RobotState | None = None
    activity: Activity | None = None
    """Last known (non-`unknown`) activity."""
    track: _Track | None = None
    recent: deque[Mission] = field(default_factory=lambda: deque(maxlen=RECENT_MISSIONS))
    resume: Mission | None = None
    """An `in_progress` mission from before a restart, until the robot confirms or ends it."""


def outcome(
    last: Activity | None, end: Activity, last_command: str | None, error_at_end: bool
) -> MissionOutcome:
    on_dock = end in ON_DOCK
    if on_dock and last in (Activity.PAUSED, Activity.STUCK):
        return MissionOutcome.MANUALLY_DOCKED
    if last_command in CANCEL_COMMANDS:
        return MissionOutcome.CANCELLED
    if Activity.STUCK in (last, end):
        return MissionOutcome.STUCK
    if error_at_end:
        return MissionOutcome.FAILED
    return MissionOutcome.COMPLETED if on_dock else MissionOutcome.CANCELLED


class Deriver:
    def __init__(self, bus: EventBus, new_id: Any = None) -> None:
        self._bus = bus
        self._new_id = new_id or (lambda: str(uuid.uuid4()))
        self._robots: dict[str, _Robot] = {}
        self._lock = asyncio.Lock()

    # -- queries ---------------------------------------------------------------------------------

    def current(self, robot_id: str) -> Mission | None:
        """The robot's in-progress mission (once started), or None."""
        r = self._robots.get(robot_id)
        mission = r.track.mission if r and r.track else None
        return mission.model_copy(deep=True) if mission else None

    def find(self, robot_id: str, mission_id: str) -> Mission | None:
        """The current mission or a recently finished one (in memory only) with this ID."""
        current = self.current(robot_id)
        if current is not None and current.id == mission_id:
            return current
        r = self._robots.get(robot_id)
        for mission in reversed(r.recent if r else ()):
            if mission.id == mission_id:
                return mission.model_copy(deep=True)
        return None

    def seen(self, robot_id: str) -> bool:
        r = self._robots.get(robot_id)
        return r is not None and r.last is not None

    def restore(self, missions: Iterable[Mission]) -> None:
        """Seed from persisted history (oldest first) before the first state arrives."""
        for mission in missions:
            r = self._robots.setdefault(mission.robot_id, _Robot())
            if mission.outcome is MissionOutcome.IN_PROGRESS:
                r.resume = mission.model_copy(deep=True)
            else:
                r.recent.append(mission.model_copy(deep=True))

    # -- state path ------------------------------------------------------------------------------

    async def update(self, after: RobotState) -> None:
        async with self._lock:
            r = self._robots.setdefault(after.robot_id, _Robot())
            before, r.last = r.last, after
            for type_, data in self._derive(r, before, after):
                await self._bus.publish(type_, after.robot_id, data, at=after.at)

    def _derive(
        self, r: _Robot, before: RobotState | None, after: RobotState
    ) -> list[tuple[EventType, BaseModel]]:
        out: list[tuple[EventType, BaseModel]] = []
        finished = self._missions(r, before, after, out)
        mission_id = (r.track.mission.id if r.track and r.track.mission else None) or (
            finished.id if finished else None
        )
        self._activity(r, after, mission_id, out)
        if not (before and before.bin and before.bin.full) and after.bin and after.bin.full:
            out.append((EventType.BIN_FULL, BinFullData()))
        for kind in ("error", "not_ready"):
            old: CodeText | None = getattr(before, kind) if before else None
            new: CodeText | None = getattr(after, kind)
            if (old.code if old else None) == (new.code if new else None):
                continue
            if old is not None:
                out.append((EventType.ERROR_CLEARED, ErrorEventData(kind=kind, **old.model_dump())))
            if new is not None:
                out.append((EventType.ERROR_RAISED, ErrorEventData(kind=kind, **new.model_dump())))
        for name, value in after.settings.items():
            prior = before.settings.get(name) if before else None
            # None is "not reported yet", not a value: first reads after connect aren't changes.
            if prior is not None and value is not None and prior != value:
                out.append(
                    (
                        EventType.SETTING_CHANGED,
                        SettingChangedData.model_validate(
                            {"setting": name, "from": prior, "to": value}
                        ),
                    )
                )
        if finished is not None:
            out.append((EventType.MISSION_FINISHED, MissionEventData(mission=finished)))
        return out

    def _missions(
        self,
        r: _Robot,
        before: RobotState | None,
        after: RobotState,
        out: list[tuple[EventType, BaseModel]],
    ) -> Mission | None:
        """Advance the mission lifecycle; returns the mission that just finished, if any."""
        ref = after.mission
        if ref is None:
            if r.resume is not None:
                self._abandon(r, out)
            track, r.track = r.track, None
            if track is None or track.mission is None:
                return None
            return self._finish(r, track, before, after)
        if r.track is None:
            r.track = _Track(opened_at=after.at, robot_ref=ref.id)
        elif r.track.mission is not None and ref.id and ref.id != r.track.robot_ref:
            if r.track.robot_ref:
                # The robot is in another mission: the tracked one ended while nobody watched
                # (e.g. the link was down). An empty ID mid-cycle isn't a change (decision #11).
                self._supersede(r, r.track, out)
                r.track = _Track(opened_at=after.at, robot_ref=ref.id)
            else:
                r.track.robot_ref = ref.id  # started before the robot reported its ID
        track = r.track
        if after.error and after.error not in track.errors:
            track.errors.append(after.error)
        echo = after.last_command
        if echo is not None and before is not None and echo != before.last_command:
            track.last_command = echo.command
        if after.activity is not Activity.UNKNOWN:
            track.last_activity = after.activity
        if track.mission is None:
            track.robot_ref = ref.id or track.robot_ref
            if after.activity in RUNNING:
                if r.resume is not None and r.resume.id == track.robot_ref:
                    track.mission, r.resume = r.resume, None
                    track.errors = track.mission.errors + [
                        e for e in track.errors if e not in track.mission.errors
                    ]
                    track.mission.errors = list(track.errors)
                    return None
                if after.activity not in STARTS:
                    return None
                if r.resume is not None:
                    self._abandon(r, out)
                track.mission = self._start(r, track, after)
                out.append((EventType.MISSION_STARTED, MissionEventData(mission=track.mission)))
        else:
            track.mission.errors = list(track.errors)
        return None

    def _abandon(self, r: _Robot, out: list[tuple[EventType, BaseModel]]) -> None:
        """The robot isn't in the restored mission any more: it ended while nobody watched."""
        mission, r.resume = r.resume, None
        assert mission is not None  # noqa: S101 - caller checked
        mission.outcome = MissionOutcome.UNKNOWN
        r.recent.append(mission)
        out.append((EventType.MISSION_FINISHED, MissionEventData(mission=mission.model_copy())))

    def _supersede(self, r: _Robot, track: _Track, out: list[tuple[EventType, BaseModel]]) -> None:
        """Finish a tracked mission the robot has left for another one, like `_abandon`."""
        mission = track.mission
        assert mission is not None  # noqa: S101 - caller checked
        mission.outcome = MissionOutcome.UNKNOWN
        r.recent.append(mission)
        out.append((EventType.MISSION_FINISHED, MissionEventData(mission=mission.model_copy())))

    def _start(self, r: _Robot, track: _Track, after: RobotState) -> Mission:
        assert after.mission is not None  # noqa: S101 - caller checked
        used = {m.id for m in r.recent}
        mission_id = track.robot_ref if track.robot_ref and track.robot_ref not in used else None
        started = after.mission.started_at or track.opened_at
        return Mission(
            id=mission_id or self._new_id(),
            robot_id=after.robot_id,
            started_at=started,
            initiator=after.mission.initiator,
            rooms_requested=_rooms(after.last_command, started),
            errors=list(track.errors),
        )

    def _finish(
        self, r: _Robot, track: _Track, before: RobotState | None, after: RobotState
    ) -> Mission:
        mission = track.mission
        assert mission is not None  # noqa: S101 - caller checked
        error_at_end = after.error is not None or (before is not None and before.error is not None)
        mission.ended_at = after.at
        mission.duration_s = max(0, int((after.at - mission.started_at).total_seconds()))
        mission.outcome = outcome(
            track.last_activity, after.activity, track.last_command, error_at_end
        )
        r.recent.append(mission)
        return mission.model_copy(deep=True)

    def _activity(
        self,
        r: _Robot,
        after: RobotState,
        mission_id: str | None,
        out: list[tuple[EventType, BaseModel]],
    ) -> None:
        """Changes between known activities. `unknown` is a transient and is skipped."""
        to, prev = after.activity, r.activity
        if to is Activity.UNKNOWN or to is prev:
            return
        r.activity = to
        if prev is None:
            return
        out.append(
            (
                EventType.ACTIVITY_CHANGED,
                ActivityChangedData.model_validate(
                    {"from": prev, "to": to, "mission_id": mission_id}
                ),
            )
        )
        # An evac happens on the dock, so leaving it isn't arriving at the dock.
        if to in ON_DOCK and prev not in ON_DOCK | {Activity.EMPTYING_BIN}:
            out.append((EventType.ROBOT_DOCKED, RobotDockedData(mission_id=mission_id)))
        if to is Activity.STUCK:
            out.append((EventType.ROBOT_STUCK, RobotStuckData(error=after.error)))


def _rooms(echo: CommandEcho | None, started: datetime) -> list[str]:
    if echo is None or not echo.room_ids:
        return []
    if echo.at is not None and echo.at < started - ECHO_SLACK:
        return []
    return list(echo.room_ids)


_DERIVERS: "weakref.WeakKeyDictionary[ServiceContext, Deriver]" = weakref.WeakKeyDictionary()
Restorer = Callable[["ServiceContext"], Awaitable[Iterable[Mission]]]
_RESTORERS: list[Restorer] = []


def restore_from(loader: Restorer) -> Restorer:
    """Register a source of persisted missions to `restore` from when the deriver starts."""
    _RESTORERS.append(loader)
    return loader


def deriver(ctx: "ServiceContext") -> Deriver | None:
    """The running service's deriver (None before the component starts)."""
    return _DERIVERS.get(ctx)


def current_mission(ctx: "ServiceContext", robot_id: str) -> Mission | None:
    d = _DERIVERS.get(ctx)
    return d.current(robot_id) if d else None


@component("derive")
async def start(ctx: "ServiceContext") -> Stop:
    d = Deriver(ctx.bus)
    for load in _RESTORERS:
        try:
            d.restore(await load(ctx))
        except Exception:
            log.exception("restoring missions from %s failed", getattr(load, "__qualname__", load))

    async def listener(session: Any, before: RobotState | None, after: RobotState) -> None:
        await d.update(after)

    remove = ctx.add_state_listener(listener)
    _DERIVERS[ctx] = d
    # Sessions start before components: pick up state that arrived before the listener did.
    for session in list(ctx.sessions.values()):
        if session.has_state and not d.seen(session.robot_id):
            await d.update(session.state)

    async def stop() -> None:
        remove()
        _DERIVERS.pop(ctx, None)

    return stop
