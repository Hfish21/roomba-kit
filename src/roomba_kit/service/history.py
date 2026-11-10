"""Local mission history.

A service component persists history from events alone: `mission.started` and
`mission.finished` upsert the `Mission` (by robot and mission ID, so a re-sent mission never
duplicates), `activity.changed` appends an `ActivityTransition`, and `error.raised` (kind
`error`) appends its code to the robot's in-progress mission. Missions are kept indefinitely.

Events are applied in seq order, each in one transaction with the `history_cursor` it advances,
so after a restart (or a crash between publish and apply) history catches up from the persisted
event log without applying anything twice. Before the deriver handles its first state it asks
history for each robot's recent missions (`derive.restore_from`), which is how a restart
mid-mission continues the same record.

Cloud linking is opportunistic and never on a local path: a background sweep asks the cloud's
mission history about recently finished, unlinked missions and sets `cloud_mission_ref`.
"""

import asyncio
import contextlib
import json
import logging
import weakref
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from roomba_kit.core.errors import KitError
from roomba_kit.core.events import ActivityChangedData, ErrorEventData, Event, EventType
from roomba_kit.core.models import (
    ActivityTransition,
    CodeText,
    Mission,
    MissionDetail,
    MissionOutcome,
)
from roomba_kit.service import derive
from roomba_kit.service.cloud import manager
from roomba_kit.service.components import Stop, component
from roomba_kit.service.store import Row, Store, format_ts, parse_ts
from roomba_kit.transport.base import CloudTransport

if TYPE_CHECKING:
    from roomba_kit.service.app import ServiceContext

log = logging.getLogger(__name__)

TYPES = (
    EventType.MISSION_STARTED,
    EventType.MISSION_FINISHED,
    EventType.ACTIVITY_CHANGED,
    EventType.ERROR_RAISED,
)
CATCH_UP_PAGE = 500
CLOUD_SWEEP_S = 15 * 60.0
CLOUD_WINDOW = timedelta(days=1)
"""How long after a mission ends the sweep keeps looking for it in cloud history."""
CLOUD_START_SLACK = timedelta(minutes=2)
"""A kit-ID mission matches a cloud record that started at most this far from it."""
CLOUD_TIMEOUT_S = 30.0
IN_PROGRESS = MissionOutcome.IN_PROGRESS.value


def _mission(row: Row) -> Mission:
    return Mission(
        id=row["id"],
        robot_id=row["robot_id"],
        started_at=parse_ts(row["started_at"]),
        ended_at=parse_ts(row["ended_at"]) if row["ended_at"] else None,
        initiator=row["initiator"],
        rooms_requested=json.loads(row["rooms_requested"]),
        outcome=row["outcome"],
        duration_s=row["duration_s"],
        errors=json.loads(row["errors"]),
        cloud_mission_ref=row["cloud_mission_ref"],
    )


def _ts(dt: datetime) -> str:
    return format_ts(dt if dt.tzinfo else dt.replace(tzinfo=UTC))


async def get_mission(store: Store, mission_id: str) -> MissionDetail | None:
    """The mission with this ID (the newest, should two robots ever share one), with its
    activity transitions in time order."""
    row = await store.fetchone(
        "SELECT * FROM missions WHERE id = ? ORDER BY started_at DESC LIMIT 1", (mission_id,)
    )
    if row is None:
        return None
    mission = _mission(row)
    moves = await store.fetchall(
        "SELECT * FROM activity_transitions WHERE robot_id = ? AND mission_id = ? ORDER BY at, id",
        (mission.robot_id, mission.id),
    )
    transitions = [
        ActivityTransition.model_validate(
            {
                "robot_id": m["robot_id"],
                "at": parse_ts(m["at"]),
                "from": m["from_activity"],
                "to": m["to_activity"],
                "mission_id": m["mission_id"],
            }
        )
        for m in moves
    ]
    return MissionDetail.model_validate({**mission.model_dump(), "transitions": transitions})


async def list_missions(
    store: Store,
    robot_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    outcome: MissionOutcome | None = None,
    limit: int = 50,
) -> list[Mission]:
    """Missions started in `[since, until)`, newest first."""
    clauses: list[str] = []
    params: list[Any] = []
    for clause, value in (
        ("robot_id = ?", robot_id),
        ("started_at >= ?", _ts(since) if since else None),
        ("started_at < ?", _ts(until) if until else None),
        ("outcome = ?", outcome.value if outcome else None),
    ):
        if value is not None:
            clauses.append(clause)
            params.append(value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = await store.fetchall(
        f"SELECT * FROM missions{where} ORDER BY started_at DESC, id DESC LIMIT ?",  # noqa: S608
        [*params, limit],
    )
    return [_mission(row) for row in rows]


async def has_robot(store: Store, robot_id: str) -> bool:
    row = await store.fetchone("SELECT 1 FROM missions WHERE robot_id = ?", (robot_id,))
    return row is not None


def cloud_ref(mission: Mission, records: list[dict[str, Any]]) -> str | None:
    """The cloud record for this mission: same mission ID, else (kit IDs) the same start time.

    Cloud payloads are parsed by `cloud_data`, the one module that knows their shape.
    """
    from roomba_kit.service.cloud_data import parse_cloud_mission

    now = datetime.now(UTC)
    parsed = [m for r in records if (m := parse_cloud_mission(mission.robot_id, r, now))]
    if any(m.mission_ref == mission.id for m in parsed):
        return mission.id
    for cloud in parsed:
        start = cloud.started_at
        if start is not None and abs(start - mission.started_at) <= CLOUD_START_SLACK:
            return cloud.mission_ref
    return None


class History:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = asyncio.Lock()

    async def cursor(self) -> int:
        row = await self._store.fetchone("SELECT seq FROM history_cursor WHERE id = 1")
        return int(row["seq"]) if row else 0

    async def catch_up(self, bus: Any, upto: int | None = None) -> int:
        """Apply every persisted event after the cursor (up to `upto`). Returns the count."""
        applied = 0
        while True:
            events, _ = await bus.read(
                await self.cursor(), types=TYPES, limit=CATCH_UP_PAGE, until=upto
            )
            for event in events:
                applied += await self.apply(event)
            if len(events) < CATCH_UP_PAGE:
                return applied

    async def apply(self, event: Event) -> int:
        """Apply one event unless the cursor is already past it. Returns 1 if applied."""
        async with self._lock, self._store.transaction():
            if event.seq <= await self.cursor():
                return 0
            await self._apply(event)
            await self._store.execute(
                "UPDATE history_cursor SET seq = ? WHERE id = 1", (event.seq,)
            )
        return 1

    async def _apply(self, event: Event) -> None:
        robot_id = event.robot_id
        if robot_id is None:
            return
        if event.type in (EventType.MISSION_STARTED, EventType.MISSION_FINISHED):
            mission = Mission.model_validate(event.data["mission"])
            if event.type is EventType.MISSION_STARTED:
                # A robot runs one mission at a time: an older open record can't still be open.
                await self._store.execute(
                    "UPDATE missions SET outcome = ?"
                    " WHERE robot_id = ? AND outcome = ? AND id != ?",
                    (MissionOutcome.UNKNOWN.value, robot_id, IN_PROGRESS, mission.id),
                )
            await self._upsert(mission)
        elif event.type is EventType.ACTIVITY_CHANGED:
            change = ActivityChangedData.model_validate(event.data)
            await self._store.execute(
                "INSERT INTO activity_transitions"
                " (robot_id, at, from_activity, to_activity, mission_id) VALUES (?, ?, ?, ?, ?)",
                (robot_id, format_ts(event.at), change.from_, change.to, change.mission_id),
            )
        elif event.type is EventType.ERROR_RAISED:
            raised = ErrorEventData.model_validate(event.data)
            if raised.kind != "error":
                return
            row = await self._store.fetchone(
                "SELECT * FROM missions WHERE robot_id = ? AND outcome = ?"
                " ORDER BY started_at DESC LIMIT 1",
                (robot_id, IN_PROGRESS),
            )
            error = CodeText(code=raised.code, text=raised.text)
            if row is not None and error not in (mission := _mission(row)).errors:
                mission.errors.append(error)
                await self._upsert(mission)

    async def _upsert(self, mission: Mission) -> None:
        row = await self._store.fetchone(
            "SELECT * FROM missions WHERE robot_id = ? AND id = ?", (mission.robot_id, mission.id)
        )
        if row is not None:
            old = _mission(row)
            reopens = mission.outcome is MissionOutcome.IN_PROGRESS
            if reopens and old.outcome is not MissionOutcome.IN_PROGRESS:
                return  # a replayed start never reopens a finished mission
            mission.errors = old.errors + [e for e in mission.errors if e not in old.errors]
            mission.cloud_mission_ref = mission.cloud_mission_ref or old.cloud_mission_ref
        await self._store.execute(
            "INSERT OR REPLACE INTO missions (id, robot_id, started_at, ended_at, initiator,"
            " rooms_requested, outcome, duration_s, errors, cloud_mission_ref)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mission.id,
                mission.robot_id,
                format_ts(mission.started_at),
                format_ts(mission.ended_at) if mission.ended_at else None,
                mission.initiator.value,
                json.dumps(mission.rooms_requested),
                mission.outcome.value,
                mission.duration_s,
                json.dumps([e.model_dump() for e in mission.errors]),
                mission.cloud_mission_ref,
            ),
        )

    async def restorable(self) -> list[Mission]:
        """Each robot's open mission and its last finished ones, oldest first, for the deriver."""
        rows = await self._store.fetchall(
            "SELECT * FROM (SELECT *, row_number() OVER"
            " (PARTITION BY robot_id, outcome = ? ORDER BY started_at DESC) AS n FROM missions)"
            " WHERE n <= ? ORDER BY started_at",
            (IN_PROGRESS, derive.RECENT_MISSIONS),
        )
        return [_mission(row) for row in rows]

    async def link_cloud(self, cloud: CloudTransport, now: datetime | None = None) -> int:
        """Set `cloud_mission_ref` on recently finished missions the cloud now shows."""
        cutoff = (now or datetime.now(UTC)) - CLOUD_WINDOW
        rows = await self._store.fetchall(
            "SELECT * FROM missions WHERE cloud_mission_ref IS NULL AND outcome != ?"
            " AND coalesce(ended_at, started_at) >= ? ORDER BY started_at",
            (IN_PROGRESS, format_ts(cutoff)),
        )
        by_robot: dict[str, list[Mission]] = {}
        for row in rows:
            by_robot.setdefault(row["robot_id"], []).append(_mission(row))
        linked = 0
        for robot_id, missions in by_robot.items():
            try:
                async with asyncio.timeout(CLOUD_TIMEOUT_S):
                    records = await cloud.mission_history(robot_id)
            except (KitError, TimeoutError) as exc:
                log.debug("cloud history lookup for %s skipped (%s)", robot_id, type(exc).__name__)
                continue
            for mission in missions:
                ref = cloud_ref(mission, records)
                if ref is None:
                    continue
                await self._store.execute(
                    "UPDATE missions SET cloud_mission_ref = ? WHERE robot_id = ? AND id = ?",
                    (ref, robot_id, mission.id),
                )
                linked += 1
        return linked


_HISTORIES: "weakref.WeakKeyDictionary[ServiceContext, History]" = weakref.WeakKeyDictionary()


def history(ctx: "ServiceContext") -> History:
    if ctx not in _HISTORIES:
        _HISTORIES[ctx] = History(ctx.store)
    return _HISTORIES[ctx]


@derive.restore_from
async def _restore(ctx: "ServiceContext") -> list[Mission]:
    # The deriver starts first: bring history up to date before it reads the open missions.
    h = history(ctx)
    await h.catch_up(ctx.bus, ctx.bus.last_seq)
    return await h.restorable()


async def _sweep(ctx: "ServiceContext", h: History, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            cloud = manager(ctx).transport()
        except KitError:
            continue  # cloud not configured or unavailable: try again next sweep
        try:
            await h.link_cloud(cloud)
        except Exception:
            log.exception("linking missions to cloud history failed")


@component("history")
async def start(ctx: "ServiceContext") -> Stop:
    h = history(ctx)
    ready = asyncio.Event()

    async def on_event(event: Event) -> None:
        await ready.wait()
        await h.apply(event)

    unsubscribe = ctx.bus.subscribe(on_event, TYPES)
    try:
        await h.catch_up(ctx.bus, ctx.bus.last_seq)
    finally:
        ready.set()
    sweep = asyncio.create_task(_sweep(ctx, h, CLOUD_SWEEP_S), name="history-cloud-sweep")

    async def stop() -> None:
        sweep.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweep
        unsubscribe()

    return stop
