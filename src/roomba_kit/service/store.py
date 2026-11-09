"""SQLite persistence in `<state>/state.db` (research R7) with numbered migrations.

The store is a thin generic layer (`execute`, `fetchall`, `fetchone`, `transaction`); feature
modules own their own queries. One connection is shared by the whole service, so statements are
serialised by a lock, and a `transaction()` block holds it until it commits or rolls back.
Timestamps are stored as fixed-width UTC text (`format_ts`), so they sort and compare as text.
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import aiosqlite

from roomba_kit.config import paths
from roomba_kit.core.errors import ErrorCode, KitError

log = logging.getLogger(__name__)

Params = Sequence[Any] | Mapping[str, Any]
Row = sqlite3.Row

V1 = """
CREATE TABLE missions (
    id TEXT NOT NULL,
    robot_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    initiator TEXT NOT NULL,
    rooms_requested TEXT NOT NULL DEFAULT '[]',
    outcome TEXT NOT NULL,
    duration_s INTEGER,
    errors TEXT NOT NULL DEFAULT '[]',
    cloud_mission_ref TEXT,
    PRIMARY KEY (robot_id, id)
);
CREATE INDEX missions_by_start ON missions (robot_id, started_at);

CREATE TABLE activity_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    robot_id TEXT NOT NULL,
    at TEXT NOT NULL,
    from_activity TEXT NOT NULL,
    to_activity TEXT NOT NULL,
    mission_id TEXT
);
CREATE INDEX activity_transitions_by_time ON activity_transitions (robot_id, at);

CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    robot_id TEXT,
    at TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX events_by_time ON events (at);
CREATE INDEX events_by_robot ON events (robot_id, seq);

-- Highest seq handed out to an event that was not persisted (position), reserved in blocks.
CREATE TABLE event_seq (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    reserved INTEGER NOT NULL
);
INSERT INTO event_seq (id, reserved) VALUES (1, 0);

CREATE TABLE rooms (
    robot_id TEXT NOT NULL,
    map_id TEXT NOT NULL,
    map_version TEXT NOT NULL,
    room_id TEXT NOT NULL,
    cloud_name TEXT,
    local_name TEXT,
    type TEXT,
    PRIMARY KEY (robot_id, map_id, map_version, room_id)
);
CREATE UNIQUE INDEX rooms_unique_name ON rooms (
    robot_id, map_version, lower(coalesce(local_name, cloud_name, 'Room ' || room_id))
);

CREATE TABLE learned_rooms (
    robot_id TEXT NOT NULL,
    map_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (robot_id, map_id, room_id)
);

CREATE TABLE cloud_cache (
    robot_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY (robot_id, kind, key)
);

CREATE TABLE verification_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    robot_id TEXT NOT NULL,
    profile_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    allow_motion INTEGER NOT NULL DEFAULT 0,
    results TEXT NOT NULL DEFAULT '[]'
);
"""

V2 = """
-- The last event seq that mission history has applied (service/history.py), so history catches
-- up from the persisted event log after a restart without applying an event twice.
CREATE TABLE history_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    seq INTEGER NOT NULL
);
INSERT INTO history_cursor (id, seq) VALUES (1, 0);
CREATE INDEX missions_by_outcome ON missions (outcome, started_at);
"""

# Append only: a migration's index + 1 is its version. Never edit one that has shipped.
MIGRATIONS: tuple[str, ...] = (V1, V2)


def format_ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class Executed:
    lastrowid: int | None
    rowcount: int


class Store:
    def __init__(self, path: Path | None = None, migrations: Sequence[str] = MIGRATIONS) -> None:
        self.path = path or paths.state_db()
        self._migrations = tuple(migrations)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._in_tx: ContextVar[bool] = ContextVar(f"store_tx_{id(self)}", default=False)
        self._tasks: set[asyncio.Task[None]] = set()

    async def open(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        for pragma in ("journal_mode = WAL", "foreign_keys = ON", "busy_timeout = 5000"):
            await conn.execute(f"PRAGMA {pragma}")
        self._conn = conn
        try:
            await self.migrate()
        except BaseException:
            await self.close()
            raise
        return self

    async def close(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> Self:
        return await self.open()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("store is not open")
        return self._conn

    async def schema_version(self) -> int:
        row = await self.fetchone("SELECT coalesce(max(version), 0) FROM schema_version")
        return int(row[0]) if row else 0

    async def migrate(self) -> int:
        """Apply pending migrations, each in its own transaction. Returns the schema version."""
        conn = self.conn
        async with self._lock:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version"
                " (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
        current = await self.schema_version()
        if current > len(self._migrations):
            raise KitError(
                ErrorCode.INTERNAL,
                f"{self.path} has schema v{current}, newer than this roomba-kit"
                f" (v{len(self._migrations)})",
            )
        for version in range(current + 1, len(self._migrations) + 1):
            stamp = format_ts(datetime.now(UTC))
            script = (
                f"BEGIN;\n{self._migrations[version - 1]}\n"
                f"INSERT INTO schema_version VALUES ({version}, '{stamp}');\nCOMMIT;"
            )
            async with self._lock:
                try:
                    await conn.executescript(script)
                except BaseException:
                    if conn.in_transaction:
                        await conn.execute("ROLLBACK")
                    raise
            log.info("state.db migrated to schema v%d", version)
        return len(self._migrations)

    @asynccontextmanager
    async def _serialised(self) -> AsyncIterator[aiosqlite.Connection]:
        if self._in_tx.get():
            yield self.conn
        else:
            async with self._lock:
                yield self.conn

    async def execute(self, sql: str, params: Params = ()) -> Executed:
        async with self._serialised() as conn, conn.execute(sql, params) as cur:
            return Executed(cur.lastrowid, cur.rowcount)

    async def executemany(self, sql: str, rows: Sequence[Params]) -> int:
        async with self._serialised() as conn, conn.executemany(sql, rows) as cur:
            return cur.rowcount

    async def fetchall(self, sql: str, params: Params = ()) -> list[Row]:
        async with self._serialised() as conn, conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def fetchone(self, sql: str, params: Params = ()) -> Row | None:
        async with self._serialised() as conn, conn.execute(sql, params) as cur:
            return await cur.fetchone()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """`BEGIN IMMEDIATE` … `COMMIT`, or `ROLLBACK` on error. Nested blocks join the outer one.

        Store calls made inside the block (in the same task) run inside the transaction.
        """
        if self._in_tx.get():
            yield
            return
        async with self._lock:
            token = self._in_tx.set(True)
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                try:
                    yield
                except BaseException:
                    await self.conn.execute("ROLLBACK")
                    raise
                await self.conn.execute("COMMIT")
            finally:
                self._in_tx.reset(token)

    async def prune_events(self, retention_days: int, now: datetime | None = None) -> int:
        """Delete events older than `retention_days`. Missions are kept indefinitely."""
        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
        result = await self.execute("DELETE FROM events WHERE at < ?", (format_ts(cutoff),))
        if result.rowcount:
            log.info("pruned %d events older than %d days", result.rowcount, retention_days)
        return result.rowcount

    def start_pruner(
        self, retention_days: int | Callable[[], int], interval_s: float = 86400.0
    ) -> asyncio.Task[None]:
        """Prune now, then every `interval_s`. The task stops when the store closes."""

        async def run() -> None:
            while True:
                days = retention_days() if callable(retention_days) else retention_days
                try:
                    await self.prune_events(days)
                except Exception:
                    log.exception("event prune failed")
                await asyncio.sleep(interval_s)

        task = asyncio.create_task(run(), name="store-prune")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
