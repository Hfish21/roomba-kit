import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from roomba_kit.core.errors import KitError
from roomba_kit.service.store import MIGRATIONS, Store, format_ts, parse_ts

BLID = "00000000000000000000000000000001"
TABLES = {
    "missions",
    "activity_transitions",
    "events",
    "event_seq",
    "rooms",
    "learned_rooms",
    "cloud_cache",
    "verification_runs",
    "schema_version",
}


async def test_schema_present_and_migrations_idempotent(tmp_kit_home: Path) -> None:
    async with Store() as store:
        assert store.path == tmp_kit_home / "state" / "state.db"
        rows = await store.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")
        assert TABLES <= {r["name"] for r in rows}
    async with Store() as store:
        assert await store.migrate() == len(MIGRATIONS)
        rows = await store.fetchall("SELECT version FROM schema_version")
        assert [r["version"] for r in rows] == list(range(1, len(MIGRATIONS) + 1))


async def test_new_migrations_apply_in_order_and_failures_roll_back(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    async with Store(db):
        pass
    extra = (*MIGRATIONS, "CREATE TABLE extra (x INTEGER);")
    async with Store(db, migrations=extra) as store:
        assert await store.schema_version() == len(extra)
    broken = (*extra, "CREATE TABLE half (x INTEGER); SELECT nonsense FROM nowhere;")
    with pytest.raises(sqlite3.OperationalError):
        await Store(db, migrations=broken).open()
    async with Store(db, migrations=extra) as store:
        assert await store.fetchone("SELECT 1 FROM sqlite_master WHERE name = 'half'") is None
    with pytest.raises(KitError):
        await Store(db, migrations=MIGRATIONS).open()


async def test_room_names_unique_case_insensitively(tmp_kit_home: Path) -> None:
    sql = (
        "INSERT INTO rooms (robot_id, map_id, map_version, room_id, cloud_name, local_name)"
        " VALUES (?, 'm', ?, ?, ?, ?)"
    )
    async with Store() as store:
        await store.execute(sql, (BLID, "v1", "1", "Kitchen", None))
        await store.execute(sql, (BLID, "v2", "1", "Kitchen", None))  # other map version
        await store.execute(sql, (BLID, "v1", "2", None, None))  # "Room 2"
        for clash in (("3", "KITCHEN", None), ("4", "x", "kitchen"), ("5", None, "room 2")):
            with pytest.raises(sqlite3.IntegrityError):
                await store.execute(sql, (BLID, "v1", *clash))


async def test_transaction_commits_and_rolls_back(tmp_kit_home: Path) -> None:
    insert = "INSERT INTO cloud_cache VALUES (?, 'rooms', ?, '{}', '')"
    async with Store() as store:
        async with store.transaction():
            await store.execute(insert, (BLID, "a"))
            async with store.transaction():  # nested joins the outer one
                await store.execute(insert, (BLID, "b"))
        with pytest.raises(RuntimeError):
            async with store.transaction():
                await store.execute(insert, (BLID, "c"))
                raise RuntimeError
        rows = await store.fetchall("SELECT key FROM cloud_cache ORDER BY key")
        assert [r["key"] for r in rows] == ["a", "b"]


async def test_prune_drops_only_old_events(tmp_kit_home: Path) -> None:
    now = datetime(2026, 11, 8, 20, 0, tzinfo=UTC)
    async with Store() as store:
        for age_days in (40, 31, 29, 0):
            at = format_ts(now - timedelta(days=age_days))
            await store.execute(
                "INSERT INTO events (type, robot_id, at, data) VALUES ('bin.full', ?, ?, '{}')",
                (BLID, at),
            )
        assert await store.prune_events(30, now=now) == 2
        assert await store.prune_events(30, now=now) == 0
        rows = await store.fetchall("SELECT at FROM events")
        assert all(now - parse_ts(r["at"]) < timedelta(days=30) for r in rows)


async def test_pruner_runs_immediately_and_stops_on_close(tmp_kit_home: Path) -> None:
    store = await Store().open()
    await store.execute(
        "INSERT INTO events (type, at, data)"
        " VALUES ('bin.full', '2000-01-01T00:00:00.000000Z', '{}')"
    )
    task = store.start_pruner(lambda: 30, interval_s=3600)
    for _ in range(50):
        if await store.fetchone("SELECT 1 FROM events") is None:
            break
        await asyncio.sleep(0.01)
    assert await store.fetchone("SELECT 1 FROM events") is None
    await store.close()
    assert task.cancelled()
