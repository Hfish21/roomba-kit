"""Cloud extras (US6, FR-041–FR-043): the live mission timeline, floor plans, cloud history and
per-mission coverage. All optional (Constitution I).

- **Timeline.** One watcher per robot whose profile declares `cloud.timeline` (verified or not),
  over the account's one shared cloud MQTT session (research R12). Watchers run only while the
  cloud is connected, restart when it reconnects or the login changes, and publish each new
  timeline entry once as a `cloud.timeline` event.
- **Records.** Floor plans and history are fetched live and cached in `cloud_cache`; while the
  cloud is down the cached copy is served with its original `retrieved_at`. Coverage for a
  finished mission never changes, so a cached copy is served without asking the cloud.

Nothing here runs on a local path: every cloud call gets its own timeout, and failures become
`cloud_unavailable(reason)`, never an error on a robot command, state or event.
"""

import asyncio
import contextlib
import logging
import weakref
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from roomba_kit.core.capabilities import Capability
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import CloudTimelineData, Event, EventType
from roomba_kit.core.models import CoverageRecord, Initiator, KitModel, MapRef, MissionOutcome
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.cloud import manager
from roomba_kit.service.components import Stop, component
from roomba_kit.service.normalize import initiator
from roomba_kit.service.store import format_ts
from roomba_kit.transport.base import CloudTransport

log = logging.getLogger(__name__)

RECONCILE_S = 5.0
"""How often watchers are checked against robots and the login (cloud status changes wake it)."""
RETRY_S = 30.0
"""How long a robot's watcher waits before restarting after its stream failed or ended."""

# -- timeline ------------------------------------------------------------------------------------

_KINDS = frozenset({"start", "travel", "room", "traversal", "evac", "fin"})


def _str(value: object) -> str | None:
    return str(value) if isinstance(value, str | int) and not isinstance(value, bool) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _reason(err: KitError) -> str:
    if err.code is ErrorCode.AUTH_FAILED:
        return ErrorCode.AUTH_FAILED.value
    return str((err.details or {}).get("reason") or err.code.value)


def _entry(raw: dict[str, Any]) -> CloudTimelineData | None:
    raw_type = _str(raw.get("type"))
    if raw_type is None:
        return None
    detail = _dict(raw.get(raw_type))
    return CloudTimelineData(
        mission_id=None,
        kind=raw_type if raw_type in _KINDS else "other",  # type: ignore[arg-type]
        room_id=_str(detail.get("rid")),
        dest=_str(detail.get("dest")) if raw_type == "travel" else None,
        raw_type=raw_type,
    )


class TimelineParser:
    """Turns one robot's repeating timeline reports into each entry exactly once.

    A report carries the in-progress entries (`event[]`) and every finished one (`finEvents[]`),
    and is re-sent every few seconds. Entries are keyed by `(type, ts)` per mission. The first
    report seen for a mission yields only its current entries (no replay of what happened before
    the kit was watching); later reports also yield finished entries missed in between.
    """

    def __init__(self) -> None:
        self.mission: str | None = None
        self._seen: set[tuple[str, int]] = set()
        self._floor: int | None = None

    def feed(self, report: dict[str, Any]) -> list[CloudTimelineData]:
        mission = _str(report.get("mission_id"))
        first = mission != self.mission or self._floor is None
        if mission != self.mission:
            self.mission, self._seen, self._floor = mission, set(), None
        current = [e for e in report.get("event") or [] if isinstance(e, dict)]
        finished = [e for e in report.get("finEvents") or [] if isinstance(e, dict)]
        candidates = [(e, True) for e in current]
        if not first:
            candidates += [(e, False) for e in finished]
        out: list[tuple[int, CloudTimelineData]] = []
        for raw, is_current in candidates:
            ts = _int(raw.get("ts")) or 0
            data = _entry(raw)
            if data is None or (data.raw_type, ts) in self._seen:
                continue
            if not is_current and self._floor is not None and ts < self._floor:
                continue
            self._seen.add((data.raw_type, ts))
            out.append((ts, data.model_copy(update={"mission_id": mission})))
        if first:  # finished entries older than this were before the kit was watching
            current_ts = [ts for ts, _ in out]
            finished_ts = [e["ts"] for e in finished if isinstance(e.get("ts"), int)]
            self._floor = min(current_ts) if current_ts else max(finished_ts, default=-1) + 1
        return [data for _, data in sorted(out, key=lambda item: item[0])]


class TimelineWatchers:
    def __init__(self, ctx: ServiceContext) -> None:
        self.ctx = ctx
        self._tasks: dict[str, tuple[CloudTransport, asyncio.Task[None]]] = {}
        self._parsers: dict[str, TimelineParser] = {}
        self._retry_at: dict[str, float] = {}
        self._wake = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._unsubscribe: Callable[[], None] | None = None

    def watching(self) -> set[str]:
        return {rid for rid, (_, task) in self._tasks.items() if not task.done()}

    async def start(self) -> None:
        self._unsubscribe = self.ctx.bus.subscribe(self._on_status, [EventType.CLOUD_STATUS])
        self._runner = asyncio.get_running_loop().create_task(self._run(), name="cloud-timeline")

    async def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        tasks = [t for t in (self._runner,) if t is not None]
        tasks += [task for _, task in self._tasks.values()]
        self._runner, self._tasks = None, {}
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _on_status(self, event: Event) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while True:
            self.reconcile()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), RECONCILE_S)
            self._wake.clear()

    def _transport(self) -> CloudTransport | None:
        try:
            return manager(self.ctx).transport()
        except KitError:
            return None

    def reconcile(self) -> None:
        """Start, stop or restart watchers to match the robots and the current login."""
        transport = self._transport()
        wanted = set()
        if transport is not None:
            for rid, session in self.ctx.sessions.items():
                if session.profile is not None and session.profile.supports(
                    Capability.CLOUD_TIMELINE
                ):
                    wanted.add(rid)
        loop = asyncio.get_running_loop()
        for rid, (bound, task) in list(self._tasks.items()):
            if rid not in wanted or bound is not transport or task.done():
                task.cancel()
                del self._tasks[rid]
        for rid in wanted - set(self._tasks):
            if loop.time() < self._retry_at.get(rid, 0.0):
                continue
            assert transport is not None  # noqa: S101 (wanted is empty otherwise)
            task = loop.create_task(self._watch(rid, transport), name=f"cloud-timeline:{rid}")
            self._tasks[rid] = (transport, task)

    async def _watch(self, rid: str, transport: CloudTransport) -> None:
        parser = self._parsers.setdefault(rid, TimelineParser())
        try:
            async for report in transport.watch_timeline(rid):
                for data in parser.feed(report):
                    await self.ctx.bus.publish(EventType.CLOUD_TIMELINE, rid, data)
        except KitError as err:
            log.info("robot %s: cloud timeline stopped (%s)", rid, _reason(err))
            with contextlib.suppress(KitError):
                manager(self.ctx).report_failure(err)
        except Exception as exc:  # never let library text out; just restart later
            log.warning("robot %s: cloud timeline failed (%s)", rid, type(exc).__name__)
        self._retry_at[rid] = asyncio.get_running_loop().time() + RETRY_S
        self._wake.set()


_WATCHERS: "weakref.WeakKeyDictionary[ServiceContext, TimelineWatchers]" = (
    weakref.WeakKeyDictionary()
)


def timeline_watchers(ctx: ServiceContext) -> TimelineWatchers | None:
    return _WATCHERS.get(ctx)


@component("cloud_timeline")
async def start(ctx: ServiceContext) -> Stop:
    watchers = TimelineWatchers(ctx)
    _WATCHERS[ctx] = watchers
    await watchers.start()
    return watchers.stop


# -- cloud records -------------------------------------------------------------------------------

CALL_TIMEOUT_S = 30.0
"""Upper bound on any one cloud REST call."""
SQFT_M2 = 0.09290304

Point = tuple[float, float]


class FloorPlanRoom(KitModel):
    id: str = Field(description="Room (region) ID on this map")
    name: str = Field(description="Kit name, else the cloud name, else `Room <id>`")
    type: str | None = Field(None, description="Room type from the cloud, e.g. `kitchen`")
    polygon: list[list[Point]] = Field(
        description="Outline as rings of [x, y] metres from the dock, the outer ring first"
    )


class FloorPlan(KitModel):
    map: MapRef
    rooms: list[FloorPlanRoom]
    retrieved_at: datetime = Field(description="When this was fetched from the cloud")


class CloudMission(KitModel):
    mission_ref: str = Field(description="Cloud mission ID; equals the robot's mission ID")
    robot_id: str
    number: int | None = Field(None, description="The robot's mission counter")
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration_s: int | None = None
    outcome: MissionOutcome
    initiator: Initiator
    area_m2: float | None = None
    rooms_requested: list[str] = Field(default_factory=list, description="Empty = whole home")
    evacs: int | None = Field(None, description="Clean Base bin emptyings during the mission")
    map: MapRef | None = Field(None, description="The mission's map, used to fetch its coverage")
    retrieved_at: datetime


class _History(KitModel):
    missions: list[CloudMission]


# -- cloud calls and the cache -------------------------------------------------------------------


def _unavailable(reason: str, message: str, robot_id: str) -> KitError:
    return KitError(ErrorCode.CLOUD_UNAVAILABLE, message, robot_id, {"reason": reason})


def _malformed(robot_id: str) -> KitError:
    return _unavailable(
        "response_malformed", "The iRobot cloud answered in an unexpected shape.", robot_id
    )


async def _load[M: BaseModel](
    ctx: ServiceContext, robot_id: str, kind: str, key: str, model: type[M]
) -> M | None:
    row = await ctx.store.fetchone(
        "SELECT json FROM cloud_cache WHERE robot_id = ? AND kind = ? AND key = ?",
        (robot_id, kind, key),
    )
    if row is None:
        return None
    try:
        return model.model_validate_json(row[0])
    except ValueError:
        log.warning("robot %s: discarding an unreadable cached %s", robot_id, kind)
        return None


async def _save(ctx: ServiceContext, robot_id: str, kind: str, key: str, value: BaseModel) -> None:
    await ctx.store.execute(
        "INSERT OR REPLACE INTO cloud_cache (robot_id, kind, key, json, retrieved_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (robot_id, kind, key, value.model_dump_json(), format_ts(datetime.now(UTC))),
    )


async def _call[T](
    ctx: ServiceContext, robot_id: str, fetch: Callable[[CloudTransport], Awaitable[T]]
) -> T:
    """Run one cloud fetch with a timeout; every failure is `cloud_unavailable(reason)`."""
    cloud = manager(ctx)
    transport = cloud.transport()
    try:
        async with asyncio.timeout(CALL_TIMEOUT_S):
            return await fetch(transport)
    except KitError as err:
        if err.code in (ErrorCode.CLOUD_UNAVAILABLE, ErrorCode.AUTH_FAILED):
            cloud.report_failure(err)
            raise _unavailable(_reason(err), err.message, robot_id) from None
        raise
    except TimeoutError:
        timeout = _unavailable("timeout", "The iRobot cloud didn't answer in time.", robot_id)
        cloud.report_failure(timeout)
        raise timeout from None
    except (TypeError, ValueError, KeyError, IndexError, AttributeError):
        raise _malformed(robot_id) from None


async def _live_or_cached[M: BaseModel](
    ctx: ServiceContext,
    robot_id: str,
    kind: str,
    key: str,
    model: type[M],
    fetch: Callable[[CloudTransport], Awaitable[M]],
) -> M:
    """Fetch and cache; while the cloud is down, the cached copy (FR-043)."""
    try:
        value = await _call(ctx, robot_id, fetch)
    except KitError as err:
        if err.code is ErrorCode.CLOUD_UNAVAILABLE and _reason(err) != "not_yet_published":
            cached = await _load(ctx, robot_id, kind, key, model)
            if cached is not None:
                return cached
        raise
    await _save(ctx, robot_id, kind, key, value)
    return value


# -- floor plan ----------------------------------------------------------------------------------


def _active_map(pmaps: list[dict[str, Any]]) -> tuple[str, str, list[dict[str, Any]]] | None:
    active = [p for p in pmaps if p.get("state") == "active"] or pmaps
    if not active:
        return None
    pmap = active[0]
    details = pmap.get("active_pmapv_details") or {}
    return str(pmap["pmap_id"]), str(pmap["active_pmapv_id"]), list(details.get("regions") or [])


def _umf_rooms(umf: dict[str, Any], map_id: str) -> dict[str, dict[str, Any]]:
    """Room ID → `{name, type, polygon}` from a stored-map UMF (polygons reference `points2d`)."""
    maps = umf.get("maps") or []
    umf_map = next((m for m in maps if (m.get("map_header") or {}).get("id") == map_id), None)
    umf_map = umf_map or (maps[0] if maps else {})
    points = {
        p["id"]: (float(p["coordinates"][0]), float(p["coordinates"][1]))
        for p in umf_map.get("points2d") or []
    }
    rooms = {}
    for region in umf_map.get("regions") or []:
        rings = (region.get("geometry") or {}).get("ids") or []
        polygon = [[points[i] for i in ring if i in points] for ring in rings]
        rooms[str(region["id"])] = {
            "name": region.get("name"),
            "type": region.get("region_type"),
            "polygon": polygon,
        }
    return rooms


def _room_sort(room_id: str) -> tuple[int, str]:
    return (int(room_id), "") if room_id.isdigit() else (1 << 30, room_id)


async def _fetch_floor_plan(robot_id: str, transport: CloudTransport) -> FloorPlan:
    active = _active_map(await transport.pmaps(robot_id))
    if active is None:
        raise KitError(
            ErrorCode.NO_MAP, "The iRobot cloud has no stored map for this robot.", robot_id
        )
    map_id, version, regions = active
    shapes = _umf_rooms(await transport.umf(robot_id, map_id, version), map_id)
    names = {str(r["id"]): r for r in regions}
    rooms = []
    for room_id in sorted(set(names) | set(shapes), key=_room_sort):
        cloud = names.get(room_id, {})
        shape = shapes.get(room_id, {})
        rooms.append(
            FloorPlanRoom(
                id=room_id,
                name=cloud.get("name") or shape.get("name") or "",
                type=cloud.get("region_type") or shape.get("type"),
                polygon=shape.get("polygon", []),
            )
        )
    return FloorPlan(
        map=MapRef(map_id=map_id, version=version), rooms=rooms, retrieved_at=datetime.now(UTC)
    )


async def _local_names(ctx: ServiceContext, robot_id: str, map_ref: MapRef) -> dict[str, str]:
    """Hook for kit room names (US3 `name_room`), read from the `rooms` table when present."""
    rows = await ctx.store.fetchall(
        "SELECT room_id, local_name FROM rooms WHERE robot_id = ? AND map_id = ?"
        " AND map_version = ? AND local_name IS NOT NULL",
        (robot_id, map_ref.map_id, map_ref.version),
    )
    return {str(row[0]): str(row[1]) for row in rows}


async def floor_plan(ctx: ServiceContext, robot_id: str) -> FloorPlan:
    plan = await _live_or_cached(
        ctx,
        robot_id,
        "floor_plan",
        "active",
        FloorPlan,
        lambda transport: _fetch_floor_plan(robot_id, transport),
    )
    local = await _local_names(ctx, robot_id, plan.map)
    rooms = [
        room.model_copy(update={"name": local.get(room.id) or room.name or f"Room {room.id}"})
        for room in plan.rooms
    ]
    return plan.model_copy(update={"rooms": rooms})


# -- cloud history -------------------------------------------------------------------------------

_OUTCOMES = {
    "ok": MissionOutcome.COMPLETED,
    "cncl": MissionOutcome.CANCELLED,
    "stuck": MissionOutcome.STUCK,
    "plcDoc": MissionOutcome.MANUALLY_DOCKED,
}


def _epoch(value: object) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if isinstance(value, int) and value > 0 else None


def _mission_map(record: dict[str, Any]) -> MapRef | None:
    info = record.get("pmaps_info")
    if not isinstance(info, list) or not info or not isinstance(info[0], dict):
        return None
    map_id, version = info[0].get("pmap_id"), info[0].get("pmapv_id")
    if not (isinstance(map_id, str) and isinstance(version, str)):
        return None
    return MapRef(map_id=map_id, version=version)


def _refs(record: dict[str, Any]) -> set[str]:
    timeline = record.get("timeline")
    refs = {
        record.get("missionId"),
        timeline.get("mission_id") if isinstance(timeline, dict) else None,
    }
    return {r for r in refs if isinstance(r, str) and r}


def parse_cloud_mission(
    robot_id: str, record: dict[str, Any], now: datetime
) -> CloudMission | None:
    refs = sorted(_refs(record))
    if not refs:
        return None
    done = record.get("done_raw") if record.get("done_raw") in _OUTCOMES else record.get("done")
    cmd = _dict(record.get("cmd"))
    regions = cmd.get("regions")
    regions = regions if isinstance(regions, list) else []
    sqft = record.get("sqft")
    duration = _int(record.get("durationM"))
    return CloudMission(
        mission_ref=_str(record.get("missionId")) or refs[0],
        robot_id=robot_id,
        number=_int(record.get("nMssn")),
        started_at=_epoch(record.get("startTime")),
        ended_at=_epoch(record.get("timestamp")),
        duration_s=duration * 60 if duration is not None else None,
        outcome=_OUTCOMES.get(str(done), MissionOutcome.UNKNOWN),
        initiator=initiator(record.get("initiator")),
        area_m2=round(sqft * SQFT_M2, 2) if isinstance(sqft, int | float) else None,
        rooms_requested=[
            str(r["region_id"]) for r in regions if isinstance(r, dict) and "region_id" in r
        ],
        evacs=_int(record.get("evacs")),
        map=_mission_map(record),
        retrieved_at=now,
    )


async def _fetch_history(robot_id: str, transport: CloudTransport) -> _History:
    now = datetime.now(UTC)
    missions = [
        m
        for r in await transport.mission_history(robot_id)
        if (m := parse_cloud_mission(robot_id, r, now)) is not None
    ]
    floor = datetime.min.replace(tzinfo=UTC)
    missions.sort(key=lambda m: m.started_at or m.ended_at or floor, reverse=True)
    return _History(missions=missions)


async def cloud_history(ctx: ServiceContext, robot_id: str, limit: int) -> list[CloudMission]:
    history = await _live_or_cached(
        ctx,
        robot_id,
        "history",
        "recent",
        _History,
        lambda transport: _fetch_history(robot_id, transport),
    )
    return history.missions[:limit]


# -- coverage ------------------------------------------------------------------------------------


def _outer_vertices(polygons: list[list[list[Point]]]) -> list[Point]:
    """Clutter polygons flattened to points: each outer ring's vertices, closing vertex dropped."""
    points: list[Point] = []
    for polygon in polygons:
        ring = polygon[0] if polygon else []
        points += ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    return points


async def _mission_refs(ctx: ServiceContext, robot_id: str, mission_id: str) -> set[str]:
    """The kit mission ID (= robot mission ID) plus its cloud ref from local history, if any."""
    row = await ctx.store.fetchone(
        "SELECT cloud_mission_ref FROM missions WHERE robot_id = ? AND id = ?",
        (robot_id, mission_id),
    )
    return {mission_id} | ({str(row[0])} if row is not None and row[0] else set())


async def _fetch_coverage(
    robot_id: str, refs: set[str], transport: CloudTransport
) -> CoverageRecord:
    from roomba_kit.transport.cloud import (
        parse_umf_layers,
    )  # lazy: keeps roombapy_prime off startup

    records = await transport.mission_history(robot_id)
    record = next((r for r in records if _refs(r) & refs), None)
    if record is None:
        raise _unavailable(
            "not_yet_published",
            "The iRobot cloud hasn't published this mission yet (it does so minutes after the "
            "robot docks, and keeps only about 30 recent missions).",
            robot_id,
        )
    map_ref = _mission_map(record)
    if map_ref is None:
        raise _malformed(robot_id)
    layers = parse_umf_layers(await transport.umf(robot_id, map_ref.map_id, map_ref.version))
    layer = next((x for x in layers if x.map_id == map_ref.map_id), layers[0] if layers else None)
    if layer is None:
        raise _malformed(robot_id)
    return CoverageRecord(
        robot_id=robot_id,
        mission_ref=_str(record.get("missionId")) or sorted(_refs(record))[0],
        map_id=layer.map_id or map_ref.map_id,
        map_version=layer.map_version or map_ref.version,
        points=layer.coverage,
        point_area_m2=layer.point_area_m2,
        dirt=layer.dirt,
        clutter=_outer_vertices(layer.clutter),
        retrieved_at=datetime.now(UTC),
    )


async def coverage(ctx: ServiceContext, robot_id: str, mission_id: str) -> CoverageRecord:
    refs = await _mission_refs(ctx, robot_id, mission_id)
    for ref in sorted(refs):
        cached = await _load(ctx, robot_id, "coverage", ref, CoverageRecord)
        if cached is not None:  # a finished mission's coverage never changes
            return cached
    record = await _call(ctx, robot_id, lambda t: _fetch_coverage(robot_id, refs, t))
    for ref in refs | {record.mission_ref}:
        await _save(ctx, robot_id, "coverage", ref, record)
    return record
