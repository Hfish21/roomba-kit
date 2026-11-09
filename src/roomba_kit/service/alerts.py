"""`alert.dock_search_timeout` (T051, FR-038): a robot has been returning to its dock for longer
than `alerts.dock_search_minutes`. Fires at most once per mission.

Robots flicker out of `returning` for a moment while docking (a `run` blip on dock contact, the
`charge` transient), so leaving `returning` only ends the search once it lasts `SETTLE_S`.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from roomba_kit.core.events import DockSearchTimeoutData, EventType
from roomba_kit.core.models import Activity, RobotState
from roomba_kit.service import derive
from roomba_kit.service.components import Stop, component
from roomba_kit.service.eventbus import EventBus

if TYPE_CHECKING:
    from roomba_kit.service.app import ServiceContext

log = logging.getLogger(__name__)

SETTLE_S = 10.0
CHECK_INTERVAL_S = 5.0
clock: Callable[[], float] = time.monotonic
"""Seconds, monotonic. Tests replace it (and `CHECK_INTERVAL_S`) to skip real waiting."""


@dataclass
class _Search:
    since: float
    mission_id: str | None
    left_at: float | None = None


class DockSearchMonitor:
    def __init__(
        self,
        bus: EventBus,
        minutes: Callable[[], float],
        mission_id: Callable[[str], str | None],
        now: Callable[[], float] | None = None,
    ) -> None:
        self._bus = bus
        self._minutes = minutes
        self._mission_id = mission_id
        self._now = now or (lambda: clock())
        self._searches: dict[str, _Search] = {}
        self._alerted: set[tuple[str, str | None]] = set()

    def observe(self, state: RobotState) -> None:
        now = self._now()
        search = self._searches.get(state.robot_id)
        if state.activity is Activity.RETURNING:
            mission_id = self._mission_id(state.robot_id)
            if search is None or search.mission_id not in (None, mission_id):
                self._searches[state.robot_id] = _Search(now, mission_id)
            else:
                search.left_at = None
                search.mission_id = search.mission_id or mission_id
        elif search is not None and search.left_at is None:
            search.left_at = now

    async def check(self) -> None:
        now, limit_s = self._now(), self._minutes() * 60
        for robot_id, search in list(self._searches.items()):
            if search.left_at is not None and now - search.left_at >= SETTLE_S:
                del self._searches[robot_id]
                if search.mission_id is None:  # no mission to key on: once per search
                    self._alerted.discard((robot_id, None))
                continue
            key = (robot_id, search.mission_id)
            if (
                search.left_at is None
                and now - search.since >= limit_s
                and key not in self._alerted
            ):
                self._alerted.add(key)
                minutes = int((now - search.since) // 60)
                data = DockSearchTimeoutData(mission_id=search.mission_id, minutes=minutes)
                await self._bus.publish(EventType.ALERT_DOCK_SEARCH_TIMEOUT, robot_id, data)


@component("alerts")
async def start(ctx: "ServiceContext") -> Stop:
    def mission_id(robot_id: str) -> str | None:
        mission = derive.current_mission(ctx, robot_id)
        return mission.id if mission else None

    monitor = DockSearchMonitor(
        ctx.bus, lambda: ctx.settings.alerts.dock_search_minutes, mission_id
    )

    async def listener(session: Any, before: RobotState | None, after: RobotState) -> None:
        monitor.observe(after)

    async def loop() -> None:
        while True:
            await asyncio.sleep(CHECK_INTERVAL_S)
            try:
                await monitor.check()
            except Exception:
                log.exception("dock search check failed")

    remove = ctx.add_state_listener(listener)
    task = asyncio.create_task(loop(), name="dock-search-alerts")

    async def stop() -> None:
        remove()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    return stop
