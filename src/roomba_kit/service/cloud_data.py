"""The live cloud mission timeline (US6, FR-041), optional like every cloud extra (Constitution I).

One watcher per robot whose profile declares `cloud.timeline` (verified or not), over the
account's one shared cloud MQTT session (research R12). Watchers run only while the cloud is
connected, restart when it reconnects or the login changes, and publish each new timeline entry
once as a `cloud.timeline` event. A failing watcher never touches a robot command, state or
event: it reports the failure to the cloud manager and retries later.
"""

import asyncio
import contextlib
import logging
import weakref
from collections.abc import Callable
from typing import Any

from roomba_kit.core.capabilities import Capability
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import CloudTimelineData, Event, EventType
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.cloud import manager
from roomba_kit.service.components import Stop, component
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
