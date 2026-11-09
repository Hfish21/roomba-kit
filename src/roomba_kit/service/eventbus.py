"""The event bus (contracts/events.md): global `seq`, persistence, fan-out, read/wait/stream.

`seq` is strictly increasing across restarts. Persisted events take their `seq` into the
`events` table. Events that aren't persisted (`position`, unless `events.persist_position`)
still draw from the same counter, and the counter's high-water mark is reserved in blocks in
`event_seq`, so a restart never reissues a seq a live consumer has already seen.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from roomba_kit.core.events import EVENT_SCHEMA, Event, EventType, encode_data
from roomba_kit.service.store import Row, Store, format_ts, parse_ts

log = logging.getLogger(__name__)

Callback = Callable[[Event], Awaitable[None]]
SEQ_BLOCK = 1000
STREAM_BUFFER = 10_000
REPLAY_PAGE = 500


@dataclass(frozen=True)
class EventFilter:
    types: frozenset[EventType] | None = None
    robot: str | None = None

    @classmethod
    def of(cls, types: Iterable[EventType] | None, robot: str | None) -> "EventFilter":
        return cls(frozenset(types) if types else None, robot)

    def matches(self, event: Event) -> bool:
        if self.robot is not None and event.robot_id != self.robot:
            return False
        return self.types is None or event.type in self.types


@dataclass(eq=False)
class _Listener:
    filter: EventFilter
    queue: asyncio.Queue[Event | None]
    overflowed: bool = False
    task: asyncio.Task[None] | None = field(default=None)


class EventBus:
    def __init__(self, store: Store, persist_position: bool = False) -> None:
        self._store = store
        self._persist_position = persist_position
        self._lock = asyncio.Lock()
        self._seq = 0
        self._reserved = 0
        self._listeners: list[_Listener] = []
        self._started = False

    async def start(self) -> None:
        """Initialise the counter from the database. Call once, after `store.open()`."""
        row = await self._store.fetchone(
            "SELECT (SELECT coalesce(max(seq), 0) FROM events),"
            " (SELECT coalesce(max(seq), 0) FROM sqlite_sequence WHERE name = 'events'),"
            " (SELECT reserved FROM event_seq WHERE id = 1)"
        )
        if row is None:
            raise RuntimeError("event_seq row missing; state.db is not migrated")
        self._reserved = int(row[2])
        self._seq = max(int(row[0]), int(row[1]), self._reserved)
        self._started = True

    async def close(self, timeout_s: float = 5.0) -> None:
        """End every stream and stop subscribers after they finish their queued events."""
        listeners, self._listeners = self._listeners, []
        for listener in listeners:
            listener.queue.put_nowait(None)
        tasks = [x.task for x in listeners if x.task is not None]
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=timeout_s)
            for task in pending:
                task.cancel()

    @property
    def last_seq(self) -> int:
        return self._seq

    def _persists(self, type_: EventType) -> bool:
        return type_ is not EventType.POSITION or self._persist_position

    async def publish(
        self,
        type_: EventType,
        robot_id: str | None,
        data: BaseModel,
        at: datetime | None = None,
    ) -> Event:
        """Validate, assign the next seq, persist (unless excluded) and fan out."""
        if not self._started:
            raise RuntimeError("EventBus.start() was not called")
        payload = encode_data(type_, data)
        async with self._lock:
            seq = self._seq + 1
            event = Event(
                schema=EVENT_SCHEMA,
                seq=seq,
                type=type_,
                robot_id=robot_id,
                at=at or datetime.now(UTC),
                data=payload,
            )
            cancelled = False
            if self._persists(type_):
                cancelled = await self._write(
                    "INSERT INTO events (seq, type, robot_id, at, data) VALUES (?, ?, ?, ?, ?)",
                    (seq, type_.value, robot_id, format_ts(event.at), json.dumps(payload)),
                )
            elif seq > self._reserved:
                cancelled = await self._write(
                    "UPDATE event_seq SET reserved = ? WHERE id = 1", (seq + SEQ_BLOCK,)
                )
                self._reserved = seq + SEQ_BLOCK
            # No await between here and the end of fan-out: see `stream`.
            self._seq = seq
            for listener in list(self._listeners):
                if listener.filter.matches(event):
                    self._deliver(listener, event)
        if cancelled:
            raise asyncio.CancelledError
        return event

    async def _write(self, sql: str, params: tuple[object, ...]) -> bool:
        """Run a counter-advancing write that can't be abandoned halfway.

        aiosqlite commits on its own thread, so cancelling the awaiting task doesn't stop the
        write. If the publisher is cancelled, wait for the write to land, report the
        cancellation, and let `publish` advance the counter first: otherwise the next publish
        would reuse this seq and every later insert would fail. Returns True when cancelled.
        """
        write = asyncio.ensure_future(self._store.execute(sql, params))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            await asyncio.wait([write])
            if write.cancelled() or write.exception() is not None:
                raise
            return True
        return False

    def _deliver(self, listener: _Listener, event: Event) -> None:
        try:
            listener.queue.put_nowait(event)
        except asyncio.QueueFull:
            # A stream that can't keep up is ended; its client resumes from `since`.
            log.warning("event stream fell %d events behind; closing it", listener.queue.qsize())
            self._remove(listener)
            listener.overflowed = True
            while not listener.queue.empty():
                listener.queue.get_nowait()
            listener.queue.put_nowait(None)

    def _add(self, flt: EventFilter, maxsize: int = 0) -> _Listener:
        listener = _Listener(flt, asyncio.Queue(maxsize))
        self._listeners.append(listener)
        return listener

    def _remove(self, listener: _Listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def subscribe(
        self,
        callback: Callback,
        types: Iterable[EventType] | None = None,
        robot: str | None = None,
    ) -> Callable[[], None]:
        """Call `callback` for each matching event, in seq order, without blocking `publish`.

        A callback that raises is logged and keeps receiving later events. Returns an
        unsubscribe function; events already queued are still delivered.
        """
        listener = self._add(EventFilter.of(types, robot))
        name = getattr(callback, "__qualname__", repr(callback))

        async def run() -> None:
            while True:
                event = await listener.queue.get()
                if event is None:
                    listener.queue.task_done()
                    return
                try:
                    await callback(event)
                except Exception:
                    log.exception("event subscriber %s failed on seq %d", name, event.seq)
                finally:
                    listener.queue.task_done()

        listener.task = asyncio.create_task(run(), name=f"event-subscriber:{name}")

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._remove(listener)
                listener.queue.put_nowait(None)

        return unsubscribe

    async def drain(self) -> None:
        """Wait until every subscriber has handled every event published so far."""
        await asyncio.gather(*(x.queue.join() for x in self._listeners if x.task is not None))

    @staticmethod
    def _event(row: Row) -> Event:
        data: dict[str, Any] = json.loads(row["data"])
        return Event(
            schema=EVENT_SCHEMA,
            seq=row["seq"],
            type=EventType(row["type"]),
            robot_id=row["robot_id"],
            at=parse_ts(row["at"]),
            data=data,
        )

    async def read(
        self,
        since: int = 0,
        robot: str | None = None,
        types: Iterable[EventType] | None = None,
        limit: int = 100,
        until: int | None = None,
    ) -> tuple[list[Event], int]:
        """Persisted events with `since < seq [<= until]`, oldest first, and the next `since`."""
        flt = EventFilter.of(types, robot)
        sql = "SELECT seq, type, robot_id, at, data FROM events WHERE seq > ?"
        params: list[Any] = [since]
        if until is not None:
            sql += " AND seq <= ?"
            params.append(until)
        if flt.robot is not None:
            sql += " AND robot_id = ?"
            params.append(flt.robot)
        if flt.types is not None:
            sql += f" AND type IN ({', '.join('?' * len(flt.types))})"
            params.extend(sorted(t.value for t in flt.types))
        sql += " ORDER BY seq LIMIT ?"
        params.append(limit)
        events = [self._event(row) for row in await self._store.fetchall(sql, params)]
        return events, events[-1].seq if events else since

    async def wait(
        self,
        since: int | None = None,
        robot: str | None = None,
        types: Iterable[EventType] | None = None,
        timeout_s: float = 30.0,
        limit: int = 100,
    ) -> list[Event]:
        """Matching events newer than `since` (default: now) as soon as any exist, else []."""
        flt = EventFilter.of(types, robot)
        since = self._seq if since is None else since
        listener = self._add(flt)  # before the read, so nothing published meanwhile is missed
        try:
            events, _ = await self.read(since, robot, types, limit)
            if events:
                return events
            async with asyncio.timeout(timeout_s):
                while True:
                    event = await listener.queue.get()
                    if event is None:
                        return []
                    if event.seq > since:
                        break
            found = [event]
            while len(found) < limit and not listener.queue.empty():
                more = listener.queue.get_nowait()
                if more is not None:
                    found.append(more)
            return found
        except TimeoutError:
            return []
        finally:
            self._remove(listener)

    async def stream(
        self,
        since: int | None = None,
        robot: str | None = None,
        types: Iterable[EventType] | None = None,
    ) -> AsyncIterator[Event]:
        """Replay persisted events after `since`, then yield live ones, with no gap or repeat.

        `since=None` means live only. The stream ends when the bus closes, or if the consumer
        falls `STREAM_BUFFER` events behind (resume with the last seq received).
        """
        listener = self._add(EventFilter.of(types, robot), maxsize=STREAM_BUFFER)
        # Every event with seq <= head was persisted (or skipped) before registration; every
        # later one reaches the queue, because publish doesn't await between setting the
        # counter and fanning out.
        head = self._seq
        last = head if since is None else since
        try:
            while last < head:
                batch, _ = await self.read(last, robot, types, REPLAY_PAGE, until=head)
                if not batch:
                    break
                for event in batch:
                    yield event
                last = batch[-1].seq
            last = max(last, head)
            while (live := await listener.queue.get()) is not None:
                if live.seq > last:
                    last = live.seq
                    yield live
        finally:
            self._remove(listener)
