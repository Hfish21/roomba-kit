import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from roomba_kit.core.events import BinFullData, Event, EventType, RobotDockedData
from roomba_kit.core.models import PositionFix
from roomba_kit.service.eventbus import EventBus
from roomba_kit.service.store import Store

I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"


def fix(robot: str = M6) -> PositionFix:
    return PositionFix(robot_id=robot, at=datetime.now(UTC), x_m=1.0, y_m=2.0, heading_rad=0.5)


@pytest.fixture
async def store(tmp_kit_home: Path) -> AsyncIterator[Store]:
    async with Store() as s:
        yield s


@pytest.fixture
async def bus(store: Store) -> AsyncIterator[EventBus]:
    b = EventBus(store)
    await b.start()
    yield b
    await b.close()


async def test_seq_monotonic_across_restart_including_position(tmp_kit_home: Path) -> None:
    seqs: list[int] = []
    for _ in range(2):
        async with Store() as store:
            bus = EventBus(store)
            await bus.start()
            seqs.append((await bus.publish(EventType.BIN_FULL, I5, BinFullData())).seq)
            seqs.append((await bus.publish(EventType.POSITION, M6, fix())).seq)
            await bus.close()
    assert seqs == sorted(set(seqs)), seqs
    async with Store() as store:  # pruning everything doesn't rewind the counter either
        await store.execute("DELETE FROM events")
        bus = EventBus(store)
        await bus.start()
        assert (await bus.publish(EventType.BIN_FULL, I5, BinFullData())).seq > seqs[-1]


async def test_position_not_persisted_by_default(store: Store, bus: EventBus) -> None:
    await bus.publish(EventType.POSITION, M6, fix())
    docked = await bus.publish(EventType.ROBOT_DOCKED, M6, RobotDockedData(mission_id="m1"))
    events, nxt = await bus.read()
    assert [e.type for e in events] == [EventType.ROBOT_DOCKED]
    assert nxt == docked.seq and events[0].data == {"mission_id": "m1"}
    keep = EventBus(store, persist_position=True)
    await keep.start()
    await keep.publish(EventType.POSITION, M6, fix())
    assert [e.type for e in (await keep.read(since=nxt))[0]] == [EventType.POSITION]


async def test_publish_validates_payload_type(bus: EventBus) -> None:
    with pytest.raises(TypeError):
        await bus.publish(EventType.BIN_FULL, I5, RobotDockedData())
    assert bus.last_seq == 0


async def test_read_filters_and_pages(bus: EventBus) -> None:
    for robot in (I5, M6, I5):
        await bus.publish(EventType.BIN_FULL, robot, BinFullData())
    await bus.publish(EventType.ROBOT_DOCKED, I5, RobotDockedData())
    events, _ = await bus.read(robot=I5, types=[EventType.BIN_FULL])
    assert [e.seq for e in events] == [1, 3]
    page, nxt = await bus.read(limit=2)
    assert [e.seq for e in page] == [1, 2] and nxt == 2
    assert (await bus.read(since=4)) == ([], 4)


async def test_subscribers_filter_order_and_isolation(bus: EventBus) -> None:
    got: list[int] = []
    m6_only: list[Event] = []

    async def boom(event: Event) -> None:
        raise RuntimeError("subscriber bug")

    async def record(event: Event) -> None:
        await asyncio.sleep(0)
        got.append(event.seq)

    async def m6(event: Event) -> None:
        m6_only.append(event)

    bus.subscribe(boom)
    bus.subscribe(record)
    unsubscribe = bus.subscribe(m6, types=[EventType.BIN_FULL], robot=M6)
    for robot in (I5, M6, M6):
        await bus.publish(EventType.BIN_FULL, robot, BinFullData())
    await bus.publish(EventType.POSITION, M6, fix())
    await bus.drain()
    assert got == [1, 2, 3, 4]
    assert [e.seq for e in m6_only] == [2, 3]
    unsubscribe()
    await bus.publish(EventType.BIN_FULL, M6, BinFullData())
    await bus.drain()
    assert len(m6_only) == 2 and got[-1] == 5


async def test_wait_returns_backlog_wakes_on_publish_and_times_out(bus: EventBus) -> None:
    first = await bus.publish(EventType.BIN_FULL, I5, BinFullData())
    assert [e.seq for e in await bus.wait(since=0, timeout_s=1)] == [first.seq]
    assert await bus.wait(since=first.seq, timeout_s=0.05) == []

    waiter = asyncio.create_task(bus.wait(since=first.seq, robot=M6, timeout_s=5))
    await asyncio.sleep(0.01)
    await bus.publish(EventType.BIN_FULL, I5, BinFullData())  # filtered out
    live = await bus.publish(EventType.POSITION, M6, fix())  # live-only still wakes it
    assert [e.seq for e in await asyncio.wait_for(waiter, 1)] == [live.seq]


async def test_stream_replays_then_goes_live_without_gaps(bus: EventBus) -> None:
    for _ in range(3):
        await bus.publish(EventType.BIN_FULL, I5, BinFullData())
    seen: list[int] = []

    async def consume() -> None:
        async for event in bus.stream(since=1):
            seen.append(event.seq)
            if event.seq == 2:  # publish while the stream is mid-replay
                await bus.publish(EventType.POSITION, M6, fix())
                await bus.publish(EventType.BIN_FULL, I5, BinFullData())
            if len(seen) == 5:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await bus.publish(EventType.BIN_FULL, I5, BinFullData())
    await asyncio.wait_for(task, 1)
    assert seen == [2, 3, 4, 5, 6]


async def test_stream_filters_and_ends_on_close(store: Store) -> None:
    bus = EventBus(store)
    await bus.start()
    await bus.publish(EventType.BIN_FULL, I5, BinFullData())
    seen: list[str | None] = []

    async def consume() -> None:
        async for event in bus.stream(since=0, robot=M6):
            seen.append(event.robot_id)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    await bus.publish(EventType.BIN_FULL, M6, BinFullData())
    await asyncio.sleep(0.01)
    await bus.close()
    await asyncio.wait_for(task, 1)
    assert seen == [M6]
