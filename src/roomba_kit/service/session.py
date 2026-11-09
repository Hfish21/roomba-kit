"""RobotSession: the single owner of one robot's local connection (Constitution II).

It supervises the connection with backoff, merges and normalizes reported state, publishes
`connection.changed` and coalesced `state.changed` events, and runs every command and setting
write for the robot through one FIFO queue (FR-030). Derived behaviour (events, history,
alerts, …) hooks in through state listeners and the EventBus, never by editing this module.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from roomba_kit.config.settings import RobotEntry
from roomba_kit.core.capabilities import COMMAND_CAPABILITIES, Capability
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import ConnectionChangedData, EventType, StateChangedData
from roomba_kit.core.models import CommandResult, ConnectionState, PositionFix, Robot, RobotState
from roomba_kit.profiles.base import ModelProfile
from roomba_kit.service.confirm import await_confirmation, spec_for
from roomba_kit.service.eventbus import EventBus
from roomba_kit.service.normalize import merge, normalize
from roomba_kit.transport.base import RobotTransport, RobotTransportFactory

log = logging.getLogger(__name__)
T = TypeVar("T")

StateListener = Callable[["RobotSession", RobotState | None, RobotState], Awaitable[None]]
"""Called on every normalized update (uncoalesced), in order, with `(session, before, after)`.

Listeners run inline on the state path, so they must be quick and must never await
`execute()`/`submit()` (that would stall state processing for a whole confirmation window).
To act on a change, publish an event or spawn a task.
"""

Rediscover = Callable[[str], Awaitable[str | None]]
"""BLID -> the robot's current address, or None when it can't be found."""

BACKOFF_MAX_S = 50.0
"""Backoff cap. Below 60 s so the worst-case reconnect (cap + connect time) meets SC-006."""

FIRST_STATE_TIMEOUT_S = 20.0
"""A connection that delivers no state this long after connecting is dropped and retried.
Seen on an i5 right after HOME-button password retrieval: it accepted the session but sent
nothing until a later reconnect."""

MISSION_SILENCE_S = 120.0
"""During a mission, a link that has delivered no state this long is reconnected to fetch a
fresh snapshot. HW-1: an i5's link stayed up but carried nothing for 30 min of cleaning."""

REDISCOVER_AFTER = 3
"""Consecutive offline connection attempts before re-locating the robot by BLID (FR-018)."""

_LINK_ERRORS = {
    ConnectionState.HELD_ELSEWHERE: (
        ErrorCode.CONNECTION_HELD_ELSEWHERE,
        "Another client holds the robot's local connection. Close the iRobot app or any "
        "other integration.",
    ),
    ConnectionState.AUTH_FAILED: (
        ErrorCode.AUTH_FAILED,
        "The robot rejected its stored password. Re-pair it with `roomba-kit robots password`.",
    ),
    ConnectionState.NOT_CONFIGURED: (
        ErrorCode.ROBOT_OFFLINE,
        "The robot has no password yet. Retrieve it with `roomba-kit robots password`.",
    ),
}


def _now() -> datetime:
    return datetime.now(UTC)


class RobotSession:
    def __init__(
        self,
        entry: RobotEntry,
        profile: ModelProfile | None,
        transport_factory: RobotTransportFactory,
        password: Callable[[], str | None],
        bus: EventBus,
        *,
        listeners: list[StateListener] | None = None,
        rediscover: Rediscover | None = None,
        on_address: Callable[[str, str], None] | None = None,
        backoff_min: float = 1.0,
        backoff_max: float = BACKOFF_MAX_S,
        first_state_timeout_s: float = FIRST_STATE_TIMEOUT_S,
        mission_silence_s: float = MISSION_SILENCE_S,
        coalesce_s: float = 1.0,
    ) -> None:
        self.robot_id = entry.id
        self.name = entry.name
        self.address = entry.address or ""
        self.profile = profile
        self._entry = entry
        self._factory = transport_factory
        self._password = password
        self._bus = bus
        self._listeners = listeners if listeners is not None else []
        self._rediscover = rediscover
        self._on_address = on_address
        self._backoff_min = backoff_min
        self._backoff_max = backoff_max
        self._first_state_timeout = first_state_timeout_s
        self._mission_silence = mission_silence_s
        self._last_fragment = 0.0
        self._got_state = asyncio.Event()
        self._coalesce_s = coalesce_s

        self.connection = ConnectionState.NOT_CONFIGURED
        self.reported: dict[str, Any] = {}
        self._state: RobotState | None = None
        self._change_seq = 0
        self._changed = asyncio.Condition()
        self._last_reconnect: datetime | None = None
        self._ever_connected = False
        self.backoff_history: list[float] = []
        """Every backoff delay slept, in order (diagnostics and tests)."""

        self._transport: RobotTransport | None = None
        self._transport_key: tuple[str, str] | None = None
        self._link_lost = asyncio.Event()
        self._lost_reason: str | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._backoff = backoff_min
        self._offline_attempts = 0
        self._fragments: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._jobs: asyncio.Queue[tuple[Callable[[], Awaitable[Any]], asyncio.Future[Any]]] = (
            asyncio.Queue()
        )
        self._pending_fields: set[str] = set()
        self._last_state_event = 0.0
        self._flush_task: asyncio.Task[None] | None = None
        self._tasks: list[asyncio.Task[None]] = []

    def __repr__(self) -> str:
        return f"RobotSession({self.robot_id!r}, {self.connection.value})"

    # -- lifecycle -------------------------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._stopping = False
        self._tasks = [
            loop.create_task(self._supervise(), name=f"session:{self.robot_id}:supervise"),
            loop.create_task(self._process(), name=f"session:{self.robot_id}:state"),
            loop.create_task(self._work(), name=f"session:{self.robot_id}:commands"),
        ]

    def _stopping_error(self) -> KitError:
        return KitError(ErrorCode.ROBOT_OFFLINE, "The service is stopping.", self.robot_id)

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        self._link_lost.set()
        for task in [*self._tasks, *([self._flush_task] if self._flush_task else [])]:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        while not self._jobs.empty():
            _, future = self._jobs.get_nowait()
            if not future.done():
                future.set_exception(self._stopping_error())
        if self._transport is not None:
            with contextlib.suppress(Exception):
                await self._transport.disconnect()

    def credentials_changed(self) -> None:
        """The robot's password changed: leave `auth_failed`/`not_configured` and reconnect."""
        self._wake.set()
        self._link_lost.set()

    # -- public view -----------------------------------------------------------------------------

    @property
    def supported(self) -> bool:
        return self.profile is not None

    def robot(self) -> Robot:
        sku = self.reported.get("sku") or self._entry.sku or ""
        firmware = self.reported.get("softwareVer") or self._entry.firmware or ""
        return Robot(
            id=self.robot_id,
            name=self.reported.get("name") or self.name or self.robot_id,
            profile_id=self.profile.id if self.profile else None,
            supported=self.supported,
            sku=str(sku),
            firmware=str(firmware),
            address=self.address,
            connection=self.connection,
            capabilities=self.profile.capability_statuses() if self.profile else [],
        )

    def require(self, capability: Capability) -> ModelProfile:
        """The profile, or `unsupported_model` / `not_supported` before any robot contact."""
        if self.profile is None:
            raise KitError(
                ErrorCode.UNSUPPORTED_MODEL,
                "This robot's model has no profile, so it can't be commanded.",
                self.robot_id,
            )
        if not self.profile.supports(capability):
            raise KitError(
                ErrorCode.NOT_SUPPORTED,
                f"{self.profile.display_name} does not support {capability.value}.",
                self.robot_id,
                {"profile": self.profile.id, "capability": capability.value},
            )
        return self.profile

    def require_link(self) -> RobotTransport:
        """The live transport, or the error that explains why there isn't one."""
        if self.connection is ConnectionState.CONNECTED and self._transport is not None:
            return self._transport
        code, message = _LINK_ERRORS.get(
            self.connection, (ErrorCode.ROBOT_OFFLINE, "The robot is unreachable right now.")
        )
        raise KitError(code, message, self.robot_id)

    # -- commands --------------------------------------------------------------------------------

    async def submit(self, job: Callable[[], Awaitable[T]]) -> T:
        """Run `job` in this robot's FIFO queue, after every earlier command or setting write."""
        if self._stopping:
            raise self._stopping_error()
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self._jobs.put_nowait((job, future))
        return await future

    async def execute(self, command: str, params: dict[str, Any] | None = None) -> CommandResult:
        """Send a command and confirm it took effect (FR-029). Capability is checked first."""
        if command not in {c.value for c in COMMAND_CAPABILITIES}:
            raise KitError(ErrorCode.INVALID_REQUEST, f"Unknown command: {command}", self.robot_id)
        profile = self.require(Capability(command))

        async def run() -> CommandResult:
            transport = self.require_link()
            spec = spec_for(command, profile)
            before, sent_at = self._state, _now()
            await transport.send_command(profile.protocol_command(command), params)
            return await await_confirmation(self, command, spec, before=before, sent_at=sent_at)

        return await self.submit(run)

    async def request_position(self, timeout: float) -> PositionFix | None:
        self.require(Capability.LIVE_POSITION)
        return await self.require_link().request_position(timeout)

    async def publish_settings(self, values: dict[str, Any]) -> None:
        """Publish protocol setting keys. Callers wanting FIFO ordering wrap this in `submit`."""
        await self.require_link().publish_settings(values)

    async def _work(self) -> None:
        """Run queued jobs one at a time. A caller that gave up never stops the worker."""
        while True:
            job, future = await self._jobs.get()
            if future.done():  # the caller was cancelled while queued
                continue
            try:
                result = await job()
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if self._stopping or (task is not None and task.cancelling()):
                    if not future.done():
                        future.set_exception(self._stopping_error())
                    raise
                if not future.done():  # the job cancelled itself; the worker carries on
                    future.cancel()
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(result)

    # -- ConfirmSource ---------------------------------------------------------------------------

    @property
    def state(self) -> RobotState:
        return self._state or RobotState(robot_id=self.robot_id, at=_now())

    @property
    def has_state(self) -> bool:
        return self._state is not None

    @property
    def change_seq(self) -> int:
        return self._change_seq

    def reconnected_since(self, at: datetime) -> bool:
        return self._last_reconnect is not None and self._last_reconnect > at

    async def wait_change(self, after_seq: int, timeout_s: float) -> None:
        async with self._changed:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._changed.wait_for(lambda: self._change_seq > after_seq),
                    max(timeout_s, 0),
                )

    async def _bump(self) -> None:
        async with self._changed:
            self._change_seq += 1
            self._changed.notify_all()

    # -- connection supervision ------------------------------------------------------------------

    def _transport_for(self, password: str) -> RobotTransport:
        key = (self.address, password)
        if self._transport is None or self._transport_key != key:
            transport = self._factory(self.robot_id, self.address, password)
            if transport is not self._transport:
                transport.on_reported(self._on_fragment)
                transport.on_connection(self._on_link)
            self._transport, self._transport_key = transport, key
        return self._transport

    def _on_fragment(self, fragment: dict[str, Any]) -> None:
        if fragment:
            self._got_state.set()
            self._last_fragment = asyncio.get_running_loop().time()
        self._fragments.put_nowait(fragment)

    def _on_link(self, connected: bool, reason: str | None) -> None:
        if not connected:
            self._lost_reason = reason
            self._link_lost.set()

    async def _set_connection(self, to: ConnectionState, reason: str | None = None) -> None:
        if to is self.connection:
            return
        previous, self.connection = self.connection, to
        log.info("robot %s: %s -> %s", self.robot_id, previous.value, to.value)
        await self._bus.publish(
            EventType.CONNECTION_CHANGED,
            self.robot_id,
            ConnectionChangedData.model_validate({"from": previous, "to": to, "reason": reason}),
        )
        await self._bump()

    async def _pause(self, delay: float, nominal: float | None = None) -> None:
        """Sleep for a backoff delay; credentials changing or `stop()` cuts it short.

        `backoff_history` records the scheduled (`nominal`) delay, even when time already
        spent (rediscovery) shortens the actual sleep.
        """
        self.backoff_history.append(delay if nominal is None else nominal)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), delay)

    async def _wait_for_credentials(self) -> None:
        await self._wake.wait()

    async def _supervise(self) -> None:
        """Keep the link up. One failure (a config write, a publish) never ends supervision."""
        while not self._stopping:
            try:
                await self._attempt()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("robot %s: connection supervisor error; retrying", self.robot_id)
                await self._pause(self._backoff_max)

    def _bump_backoff(self) -> float:
        delay = self._backoff
        self._backoff = min(self._backoff * 2, self._backoff_max)
        return delay

    async def _attempt(self) -> None:
        # Clear both signals *before* reading the password, so a credentials change that
        # arrives at any later point is seen and causes a reconnect with the new password.
        self._wake.clear()
        self._link_lost.clear()
        self._got_state.clear()
        self._lost_reason = None
        password = self._password()
        if not password or not self.address:
            await self._set_connection(ConnectionState.NOT_CONFIGURED)
            await self._wait_for_credentials()
            return
        await self._set_connection(ConnectionState.CONNECTING)
        transport = self._transport_for(password)
        try:
            await transport.connect()
        except KitError as exc:
            if exc.code is ErrorCode.AUTH_FAILED:
                await self._set_connection(ConnectionState.AUTH_FAILED, exc.code.value)
                await self._wait_for_credentials()
                self._backoff = self._backoff_min
                return
            held = exc.code is ErrorCode.CONNECTION_HELD_ELSEWHERE
            state = ConnectionState.HELD_ELSEWHERE if held else ConnectionState.OFFLINE
            await self._set_connection(state, exc.code.value)
            delay = nominal = self._bump_backoff()
            if not held:
                self._offline_attempts += 1
                if self._offline_attempts % REDISCOVER_AFTER == 0:
                    # Rediscovery takes seconds; it counts toward this pause, not on top of it.
                    loop = asyncio.get_running_loop()
                    began = loop.time()
                    await self._relocate()
                    delay = max(0.0, delay - (loop.time() - began))
            await self._pause(delay, nominal)
            return

        self._offline_attempts = 0
        self._backoff = self._backoff_min
        if self._ever_connected:
            self._last_reconnect = _now()
        self._ever_connected = True
        await self._set_connection(ConnectionState.CONNECTED)
        if not await self._first_state_arrives():
            log.warning("robot %s: connected but sent no state; reconnecting", self.robot_id)
            with contextlib.suppress(Exception):
                await transport.disconnect()
            await self._set_connection(ConnectionState.OFFLINE, "no_state")
            await self._pause(self._backoff_min)
            return
        if not await self._hold_link():
            log.warning(
                "robot %s: no state for %gs mid-mission; reconnecting",
                self.robot_id,
                self._mission_silence,
            )
            with contextlib.suppress(Exception):
                await transport.disconnect()
            await self._set_connection(ConnectionState.OFFLINE, "silent")
            return
        if self._stopping:
            return
        reason = self._lost_reason
        if reason == ErrorCode.AUTH_FAILED.value:
            await self._set_connection(ConnectionState.AUTH_FAILED, reason)
            await self._wait_for_credentials()
        elif reason == ErrorCode.CONNECTION_HELD_ELSEWHERE.value:
            await self._set_connection(ConnectionState.HELD_ELSEWHERE, reason)
            await self._pause(self._bump_backoff())
        elif reason is None:  # credentials changed while connected: reconnect with them
            with contextlib.suppress(Exception):
                await transport.disconnect()
        else:
            await self._set_connection(ConnectionState.OFFLINE, reason)
            await self._pause(self._bump_backoff())

    async def _hold_link(self) -> bool:
        """Wait while the link is up. False if it went silent during a mission."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.wait_for(self._link_lost.wait(), self._mission_silence / 4)
                return True
            except TimeoutError:
                pass
            mission = self._state is not None and self._state.mission is not None
            if mission and loop.time() - self._last_fragment > self._mission_silence:
                return False

    async def _first_state_arrives(self) -> bool:
        """True once the robot reports state (or the link drops); False after the timeout."""
        state = asyncio.ensure_future(self._got_state.wait())
        lost = asyncio.ensure_future(self._link_lost.wait())
        try:
            done, _ = await asyncio.wait(
                {state, lost},
                timeout=self._first_state_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            state.cancel()
            lost.cancel()
        return bool(done)

    async def _relocate(self) -> None:
        if self._rediscover is None:
            return
        try:
            address = await self._rediscover(self.robot_id)
        except Exception:
            log.warning("robot %s: rediscovery failed", self.robot_id, exc_info=True)
            return
        if address and address != self.address:
            log.info("robot %s: found at a new address", self.robot_id)
            self.address = address
            if self._on_address is not None:
                try:
                    self._on_address(self.robot_id, address)
                except Exception:
                    log.warning("robot %s: couldn't save the new address", self.robot_id)

    # -- state -----------------------------------------------------------------------------------

    async def _process(self) -> None:
        while True:
            fragment = await self._fragments.get()
            self.reported = merge(self.reported, fragment)
            if self.profile is None:
                continue
            before = self._state
            after = normalize(self.robot_id, self.reported, self.profile, _now())
            self._state = after
            await self._bump()
            for listener in list(self._listeners):
                try:
                    await listener(self, before, after)
                except Exception:
                    log.exception("state listener failed for robot %s", self.robot_id)
            self._note_changes(before, after)

    def _note_changes(self, before: RobotState | None, after: RobotState) -> None:
        old = before.model_dump(exclude={"at"}) if before else {}
        new = after.model_dump(exclude={"at"})
        changed = {k for k, v in new.items() if old.get(k) != v}
        if not changed:
            return
        self._pending_fields |= changed
        if self._flush_task is None or self._flush_task.done():
            loop = asyncio.get_running_loop()
            delay = max(0.0, self._last_state_event + self._coalesce_s - loop.time())
            self._flush_task = loop.create_task(self._flush(delay))

    async def _flush(self, delay: float) -> None:
        # Loop until nothing is pending: fields can arrive while a publish is in flight, and
        # `_note_changes` won't schedule another flush while this task is still running.
        while True:
            if delay:
                await asyncio.sleep(delay)
            fields, self._pending_fields = sorted(self._pending_fields), set()
            self._last_state_event = asyncio.get_running_loop().time()
            if fields and self._state is not None:
                await self._bus.publish(
                    EventType.STATE_CHANGED,
                    self.robot_id,
                    StateChangedData(changed=fields, state=self._state),
                )
            if not self._pending_fields:
                return
            delay = self._coalesce_s
