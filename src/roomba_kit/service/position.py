"""Live position polling (research R13, FR-039, FR-040).

For robots whose profile declares `live_position`, a per-robot task asks for a fix every
`POLL_INTERVAL_S` while the robot is `cleaning` or `returning`, and publishes each fix as a
`position` event (live only unless `events.persist_position`). "No fix" is not an error and
not an event.

A robot that has never answered gets `GIVE_UP_AFTER` consecutive timeouts, then isn't asked
again for the rest of that mission. Once it has answered, timeouts never stop polling.

Requests go straight to the session, not through its FIFO command queue: a position request
changes nothing on the robot, needs no ordering against commands, and putting a 2 s request
in the queue would delay every command behind it (FR-040).
"""

import asyncio
import contextlib
import logging

from roomba_kit.core.capabilities import Capability
from roomba_kit.core.errors import KitError
from roomba_kit.core.events import EventType
from roomba_kit.core.models import Activity, RobotState
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.components import Stop, component
from roomba_kit.service.session import RobotSession

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 2.0
"""0.5 Hz: the m6 answered about every 2 s while cleaning (discovery §3)."""
POLL_TIMEOUT_S = 2.0
GIVE_UP_AFTER = 3
"""Consecutive timeouts, on a robot that has never answered, before giving up for the mission."""
ACTIVE = frozenset({Activity.CLEANING, Activity.RETURNING})


def _mission_id(state: RobotState) -> str | None:
    return state.mission.id if state.mission is not None else None


class PositionPoller:
    def __init__(
        self,
        ctx: ServiceContext,
        *,
        interval_s: float = POLL_INTERVAL_S,
        timeout_s: float = POLL_TIMEOUT_S,
        give_up_after: int = GIVE_UP_AFTER,
    ) -> None:
        self.ctx = ctx
        self.interval_s = interval_s
        self.timeout_s = timeout_s
        self.give_up_after = give_up_after
        self.answered: set[str] = set()
        """Robots that have answered at least once (with a fix or "no fix")."""
        self.given_up: dict[str, str | None] = {}
        """Robot → the mission ID it stopped being polled for."""
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def polling(self, robot_id: str) -> bool:
        task = self._tasks.get(robot_id)
        return task is not None and not task.done()

    async def on_state(
        self, session: RobotSession, before: RobotState | None, after: RobotState
    ) -> None:
        self.update(session, after)

    def update(self, session: RobotSession, state: RobotState) -> None:
        """Start or stop the robot's poller for its new state. Never awaits (listener rule)."""
        profile = session.profile
        if profile is None or not profile.supports(Capability.LIVE_POSITION):
            return
        rid = session.robot_id
        mission = _mission_id(state)
        if rid in self.given_up:
            gave_up_on = self.given_up[rid]
            if mission is None or (mission and gave_up_on and mission != gave_up_on):
                del self.given_up[rid]  # that mission is over
        wanted = state.activity in ACTIVE and rid not in self.given_up
        if wanted and not self.polling(rid):
            self._tasks[rid] = asyncio.get_running_loop().create_task(
                self._poll(session), name=f"position:{rid}"
            )
        elif not wanted and (task := self._tasks.pop(rid, None)) is not None:
            task.cancel()

    async def stop(self) -> None:
        tasks, self._tasks = list(self._tasks.values()), {}
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _poll(self, session: RobotSession) -> None:
        rid = session.robot_id
        loop = asyncio.get_running_loop()
        misses = 0
        while self.ctx.sessions.get(rid) is session:
            started = loop.time()
            try:
                fix = await session.request_position(self.timeout_s)
            except TimeoutError:
                if rid not in self.answered:
                    misses += 1
                    if misses >= self.give_up_after:
                        self.given_up[rid] = _mission_id(session.state)
                        log.info(
                            "robot %s never answered a position request; not asking again "
                            "this mission",
                            rid,
                        )
                        return
            except KitError as exc:  # link down: keep trying at the same cadence
                log.debug("robot %s: position request failed: %s", rid, exc.code.value)
            except Exception:
                log.exception("robot %s: position request failed", rid)
            else:
                self.answered.add(rid)
                if fix is not None:
                    await self.ctx.bus.publish(EventType.POSITION, rid, fix)
            await asyncio.sleep(max(0.0, self.interval_s - (loop.time() - started)))


@component("position")
async def start(ctx: ServiceContext) -> Stop:
    poller = PositionPoller(
        ctx, interval_s=POLL_INTERVAL_S, timeout_s=POLL_TIMEOUT_S, give_up_after=GIVE_UP_AFTER
    )
    remove = ctx.add_state_listener(poller.on_state)
    for session in list(ctx.sessions.values()):
        if session.has_state:
            poller.update(session, session.state)

    async def stop() -> None:
        remove()
        await poller.stop()

    return stop
