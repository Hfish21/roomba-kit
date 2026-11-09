"""Rooms: cloud import, local names, learned IDs, resolution and stale-map protection.

FR-008 (import), FR-031 (stale or empty requests are refused), FR-032 (learn room IDs without
the cloud), FR-043 (imported rooms stay usable while the cloud is down).

Rooms are stored per robot and map version. A room's effective name is its local name, then
its cloud name, then `Room <id>`; names are unique per robot and map version, case-insensitively
(enforced by an index on `rooms`). Learned rooms are IDs seen in the robot's room-clean echoes:
`learned_rooms` keeps when each was seen, and each is also stored as an unnamed room on the map
version it was seen on, so it goes stale like any other room when the map changes.

The protocol shapes of a room clean (`pmap_id`, `regions`, …) and of the cloud's pmaps payload
are built and parsed only here; operations and surfaces see kit models.
"""

import json
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import MapRef, RobotState, Room
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.cloud import manager
from roomba_kit.service.components import Stop, component
from roomba_kit.service.session import RobotSession
from roomba_kit.service.store import Store, format_ts, parse_ts

CACHE_KIND = "pmap"
"""`cloud_cache.kind` for an imported map: key is the map ID, json the cloud's map record."""

Passes = Literal["auto", "one", "two"]


@dataclass(frozen=True)
class RoomSet:
    map: MapRef | None
    usable: list[Room]
    """Rooms on the robot's current map version (stored or learned): what requests resolve to."""
    stale: list[Room]
    """Stored rooms for another map or version: referencing one is `map_changed`."""
    retrieved_at: datetime | None
    """When `map`'s rooms were last imported from the cloud."""


def sort_key(room: Room) -> tuple[int, str]:
    return (int(room.room_id), "") if room.room_id.isdigit() else (1 << 31, room.room_id)


async def stored_rooms(store: Store, robot_id: str) -> list[Room]:
    rows = await store.fetchall("SELECT * FROM rooms WHERE robot_id = ?", (robot_id,))
    return [Room.model_validate(dict(r)) for r in rows]


async def cached_map(
    store: Store, robot_id: str, map_id: str
) -> tuple[dict[str, Any], datetime | None]:
    """The last imported cloud record for `map_id` and its retrieval time, or `({}, None)`."""
    row = await store.fetchone(
        "SELECT json, retrieved_at FROM cloud_cache WHERE robot_id = ? AND kind = ? AND key = ?",
        (robot_id, CACHE_KIND, map_id),
    )
    if row is None:
        return {}, None
    return json.loads(row["json"]), parse_ts(row["retrieved_at"])


def current_map(session: RobotSession) -> MapRef | None:
    return session.state.map if session.has_state else None


def require_map(session: RobotSession) -> MapRef:
    """The robot's current map, or why it can't be known (`robot_offline`, `no_map`, …)."""
    if not session.has_state:
        session.require_link()
        raise KitError(
            ErrorCode.ROBOT_OFFLINE, "The robot hasn't reported its state yet.", session.robot_id
        )
    if session.state.map is None:
        raise KitError(
            ErrorCode.NO_MAP,
            "The robot has no stored map, so it can't clean by room. Run a mapping run in the "
            "iRobot app (or `roomba-kit train`) first.",
            session.robot_id,
        )
    return session.state.map


async def room_set(store: Store, session: RobotSession) -> RoomSet:
    rows = await stored_rooms(store, session.robot_id)
    current = current_map(session)
    if current is None and rows:  # no state yet: show the newest stored map
        newest = max(rows, key=lambda r: r.map_version)
        current = MapRef(map_id=newest.map_id, version=newest.map_version)
    if current is None:
        return RoomSet(None, [], [], None)
    here = (current.map_id, current.version)
    _, retrieved_at = await cached_map(store, session.robot_id, current.map_id)
    return RoomSet(
        current,
        sorted((r for r in rows if (r.map_id, r.map_version) == here), key=sort_key),
        sorted((r for r in rows if (r.map_id, r.map_version) != here), key=sort_key),
        retrieved_at,
    )


def _listing(rooms: Iterable[Room]) -> list[dict[str, str]]:
    return [{"id": r.room_id, "name": r.name} for r in rooms]


def match(rooms: list[Room], ref: str) -> Room | None:
    """The room `ref` names: an ID, else an effective name (case-insensitive).

    A cloud name shared by several rooms is stored as `Name (<id>)`, so the bare name is
    `ambiguous_name` with the candidates.
    """
    for room in rooms:
        if room.room_id == ref:
            return room
    wanted = ref.strip().casefold()
    exact = [r for r in rooms if r.name.casefold() == wanted]
    if exact:
        return exact[0]
    suffixed = [r for r in rooms if r.name.casefold() == f"{wanted} ({r.room_id})"]
    if len(suffixed) == 1:
        return suffixed[0]
    if suffixed:
        raise KitError(
            ErrorCode.AMBIGUOUS_NAME,
            f"{len(suffixed)} rooms are named {ref!r}; use a room ID.",
            details={"candidates": _listing(suffixed)},
        )
    return None


async def resolve(store: Store, session: RobotSession, refs: Iterable[str]) -> list[Room]:
    """The room on the robot's current map for each ref, in order."""
    require_map(session)
    rooms = await room_set(store, session)
    found: list[Room] = []
    for ref in refs:
        room = match(rooms.usable, ref)
        if room is None:
            raise _missing(session, rooms, ref)
        found.append(room)
    return found


def _missing(session: RobotSession, rooms: RoomSet, ref: str) -> KitError:
    old = match(rooms.stale, ref)
    if old is not None and rooms.map is not None:
        return KitError(
            ErrorCode.MAP_CHANGED,
            f"Room {ref!r} belongs to map version {old.map_version}, but the robot is now on "
            f"{rooms.map.version}. Re-import rooms (`roomba-kit rooms import`) or name them again.",
            session.robot_id,
            {
                "current": rooms.map.model_dump(),
                "requested": {"map_id": old.map_id, "version": old.map_version},
            },
        )
    return KitError(
        ErrorCode.ROOM_NOT_FOUND,
        f"No room {ref!r} on the robot's current map.",
        session.robot_id,
        {"valid": _listing(rooms.usable)},
    )


# -- naming --------------------------------------------------------------------------------------


async def name_room(store: Store, session: RobotSession, room_id: str, name: str) -> Room:
    """Give a room on the current map a local name (works without the cloud)."""
    require_map(session)
    rooms = await room_set(store, session)
    room = next((r for r in rooms.usable if r.room_id == room_id), None)
    if room is None:
        raise _missing(session, rooms, room_id)
    name = name.strip()
    if not name or name.isdigit():
        raise KitError(
            ErrorCode.INVALID_REQUEST,
            "A room name must be non-empty and can't be only digits (those are room IDs).",
            session.robot_id,
        )
    clash = [
        r for r in rooms.usable if r.room_id != room_id and r.name.casefold() == name.casefold()
    ]
    if clash:
        raise KitError(
            ErrorCode.INVALID_REQUEST,
            f"Room {clash[0].room_id} is already named {clash[0].name!r} on this map.",
            session.robot_id,
            {"conflict": _listing(clash)[0]},
        )
    try:
        await store.execute(
            "INSERT INTO rooms (robot_id, map_id, map_version, room_id, local_name)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (robot_id, map_id, map_version, room_id)"
            " DO UPDATE SET local_name = excluded.local_name",
            (room.robot_id, room.map_id, room.map_version, room.room_id, name),
        )
    except sqlite3.IntegrityError:
        raise KitError(
            ErrorCode.INVALID_REQUEST,
            f"Another room on this map version is already named {name!r}.",
            session.robot_id,
        ) from None
    return room.model_copy(update={"local_name": name})


# -- cloud import --------------------------------------------------------------------------------


def _malformed() -> KitError:
    return KitError(
        ErrorCode.CLOUD_UNAVAILABLE,
        "The iRobot cloud returned map data the kit doesn't understand.",
        details={"reason": "response_malformed"},
    )


def _pick(maps: list[dict[str, Any]], current: MapRef | None) -> dict[str, Any]:
    if current is not None:
        for record in maps:
            if record.get("pmap_id") == current.map_id:
                return record
    active = [m for m in maps if m.get("state") == "active"]
    return (active or maps)[0]


def _regions(record: Mapping[str, Any]) -> tuple[MapRef, list[tuple[str, str | None, str | None]]]:
    """(map, [(room_id, cloud name, type)]) from one cloud map record."""
    try:
        details = record["active_pmapv_details"]
        version = record.get("active_pmapv_id") or details["active_pmapv"]["pmapv_id"]
        ref = MapRef(map_id=str(record["pmap_id"]), version=str(version))
        regions = [
            (
                str(r["id"]),
                str(r.get("name") or "").strip() or None,
                r.get("region_type") if r.get("region_type") not in (None, "unspecified") else None,
            )
            for r in details.get("regions") or []
        ]
    except (KeyError, TypeError, AttributeError):
        raise _malformed() from None
    return ref, regions


def _unique_names(
    regions: list[tuple[str, str | None, str | None]], local: dict[str, str]
) -> list[tuple[str, str | None, str | None]]:
    """Make effective names unique: every room sharing a cloud name becomes `Name (<id>)`."""
    taken = {name.casefold() for name in local.values()}
    counts = Counter(
        (cloud_name or f"Room {room_id}").casefold()
        for room_id, cloud_name, _ in regions
        if room_id not in local
    )
    out = []
    for room_id, cloud_name, kind in regions:
        base = cloud_name or f"Room {room_id}"
        if room_id not in local and (counts[base.casefold()] > 1 or base.casefold() in taken):
            cloud_name = f"{base} ({room_id})"
        out.append((room_id, cloud_name, kind))
    return out


async def import_rooms(ctx: ServiceContext, session: RobotSession) -> tuple[int, MapRef]:
    """Replace the robot's stored rooms with the cloud's current map (FR-008, FR-043).

    Local names on that same map version are kept; rooms of other versions are dropped.
    """
    cloud = manager(ctx)
    transport = cloud.transport()
    try:
        maps = await transport.pmaps(session.robot_id)
    except KitError as err:
        cloud.report_failure(err)
        raise
    if not isinstance(maps, list) or not all(isinstance(m, dict) for m in maps):
        raise _malformed()
    if not maps:
        raise KitError(
            ErrorCode.NO_MAP,
            "The iRobot account has no stored map for this robot.",
            session.robot_id,
        )
    record = _pick(maps, current_map(session))
    ref, regions = _regions(record)
    rid = session.robot_id
    async with ctx.store.transaction():
        rows = await ctx.store.fetchall(
            "SELECT room_id, local_name FROM rooms WHERE robot_id = ? AND map_id = ?"
            " AND map_version = ? AND local_name IS NOT NULL",
            (rid, ref.map_id, ref.version),
        )
        ids = {room_id for room_id, _, _ in regions}
        local = {r["room_id"]: r["local_name"] for r in rows if r["room_id"] in ids}
        await ctx.store.execute("DELETE FROM rooms WHERE robot_id = ?", (rid,))
        await ctx.store.executemany(
            "INSERT INTO rooms (robot_id, map_id, map_version, room_id, cloud_name, local_name,"
            " type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (rid, ref.map_id, ref.version, room_id, name, local.get(room_id), kind)
                for room_id, name, kind in _unique_names(regions, local)
            ],
        )
        await ctx.store.execute(
            "INSERT OR REPLACE INTO cloud_cache (robot_id, kind, key, json, retrieved_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (rid, CACHE_KIND, ref.map_id, json.dumps(record), format_ts(datetime.now(UTC))),
        )
    return len(regions), ref


# -- room clean ----------------------------------------------------------------------------------


def _region_params(passes: Passes) -> dict[str, bool]:
    return {"noAutoPasses": passes != "auto", "twoPass": passes == "two"}


async def clean_params(
    store: Store,
    session: RobotSession,
    rooms: list[Room],
    *,
    ordered: bool,
    passes: Mapping[str, Passes] | None = None,
) -> dict[str, Any]:
    """The protocol params of a room clean, each room once. `passes` is keyed by room ID."""
    rooms = list({room.room_id: room for room in rooms}.values())
    if not rooms:  # the robot would treat an empty list as the whole home
        raise KitError(ErrorCode.INVALID_REQUEST, "No rooms given.", session.robot_id)
    current = require_map(session)
    record, _ = await cached_map(store, session.robot_id, current.map_id)
    echo = session.state.last_command
    if record.get("active_pmapv_id") == current.version and record.get("user_pmapv_id"):
        user_version = str(record["user_pmapv_id"])
    elif echo is not None and echo.map_id == current.map_id and echo.map_version:
        user_version = echo.map_version
    else:
        user_version = current.version
    regions = []
    for room in rooms:
        region: dict[str, Any] = {"region_id": room.room_id, "type": "rid"}
        if passes and room.room_id in passes:
            region["params"] = _region_params(passes[room.room_id])
        regions.append(region)
    return {
        "pmap_id": current.map_id,
        "user_pmapv_id": user_version,
        "ordered": 1 if ordered else 0,
        "regions": regions,
    }


# -- learned rooms (FR-032) ----------------------------------------------------------------------


async def record_learned(
    store: Store,
    robot_id: str,
    map_id: str,
    room_ids: Iterable[str],
    at: datetime,
    version: str | None = None,
) -> None:
    """Note when each room was seen; with `version`, also store it as a room of that version."""
    ids = list(dict.fromkeys(room_ids))
    stamp = format_ts(at)
    async with store.transaction():
        await store.executemany(
            "INSERT INTO learned_rooms (robot_id, map_id, room_id, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT (robot_id, map_id, room_id) DO UPDATE SET"
            " first_seen = min(first_seen, excluded.first_seen),"
            " last_seen = max(last_seen, excluded.last_seen)",
            [(robot_id, map_id, room_id, stamp, stamp) for room_id in ids],
        )
        if version is not None:  # IGNORE: already stored, or its placeholder name is taken
            await store.executemany(
                "INSERT OR IGNORE INTO rooms (robot_id, map_id, map_version, room_id)"
                " VALUES (?, ?, ?, ?)",
                [(robot_id, map_id, version, room_id) for room_id in ids],
            )


class LearnedRooms:
    """Records the room IDs of every room clean the robot echoes, from any client."""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def on_state(
        self, session: RobotSession, before: RobotState | None, after: RobotState
    ) -> None:
        echo = after.last_command
        if echo is None or not echo.room_ids:
            return
        if before is not None and before.last_command == echo:
            return
        if session.profile is not None and echo.command != session.profile.protocol_command(
            "clean_rooms"
        ):
            return  # a later command echo merged over a room clean's fields
        current = after.map
        map_id = echo.map_id or (current.map_id if current else None)
        if map_id is None:
            return
        # The robot keeps its last echo indefinitely, so one replayed at startup may predate a
        # map change: it's tied to the current version only if it names that version.
        live = before is not None or echo.map_version in (None, current and current.version)
        on_current = current is not None and current.map_id == map_id and live
        # One small write on the state path; never a command (listener rule).
        await record_learned(
            self.store,
            session.robot_id,
            map_id,
            echo.room_ids,
            echo.at or after.at,
            current.version if on_current and current else None,
        )


@component("learned_rooms")
async def start(ctx: ServiceContext) -> Stop:
    learned = LearnedRooms(ctx.store)
    remove = ctx.add_state_listener(learned.on_state)
    for session in list(ctx.sessions.values()):
        if session.has_state:
            await learned.on_state(session, None, session.state)

    async def stop() -> None:
        remove()

    return stop
