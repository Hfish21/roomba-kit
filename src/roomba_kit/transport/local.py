"""The local MQTT adapter over roombapy 2.0.2, and the only module that imports `roombapy`.

Single-shot by design: `connect()` opens one session or raises. roombapy runs its own reconnect
supervisor once connected; when the session drops, this adapter tears the client down (which
cancels that supervisor) and reports `(False, reason)`, where `reason` is an `ErrorCode` value.
The kit's session is the only thing that reconnects.
"""

import asyncio
import logging
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any

from roombapy import (
    RobotPosition,
    RoombaAuthError,
    RoombaClient,
    RoombaError,
    RoombaPassword,
    RoombaScopeError,
    RrtpUnsupportedError,
)

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import PositionFix
from roomba_kit.transport import discovery
from roomba_kit.transport.base import ConnectionCallback, ReportedCallback

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 30.0
"""roombapy needs ~18 s to give up on an unreachable host (three attempts inside `connect()`)."""
HELD_PROBE_TIMEOUT_S = 2.0


def to_position_fix(robot_id: str, pose: RobotPosition) -> PositionFix:
    """roombapy positions are already metres and radians, dock-origin (rrtp.RobotPosition)."""
    at = datetime.fromtimestamp(pose.timestamp, UTC) if pose.timestamp else datetime.now(UTC)
    return PositionFix(
        robot_id=robot_id,
        at=at,
        x_m=pose.x,
        y_m=pose.y,
        heading_rad=pose.theta,
        map_id=pose.pmap_id,
        map_version=pose.pmapv_id,
    )


class LocalTransport:
    """A `RobotTransport` for one robot; the class itself is a `RobotTransportFactory`."""

    def __init__(
        self,
        robot_id: str,
        address: str,
        password: str,
        *,
        connect_timeout: float = CONNECT_TIMEOUT_S,
    ) -> None:
        self.robot_id = robot_id
        self.address = address
        self._password = password
        self._connect_timeout = connect_timeout
        self._client: RoombaClient | None = None
        self._reported_cbs: list[ReportedCallback] = []
        self._connection_cbs: list[ConnectionCallback] = []
        self._tasks: set[asyncio.Task[None]] = set()

    def __repr__(self) -> str:
        return f"LocalTransport({self.robot_id!r}, {self.address!r})"

    async def connect(self) -> None:
        if self._client is not None:
            return
        client = RoombaClient(self.address, self.robot_id, self._password)
        client.register_on_message_callback(self._on_message)
        client.register_on_connection_state_callback(
            lambda state, _error: self._on_state(client, state)
        )
        try:
            async with asyncio.timeout(self._connect_timeout):
                await client.connect()
        except RoombaAuthError:
            raise KitError(
                ErrorCode.AUTH_FAILED, "The robot rejected its password.", self.robot_id
            ) from None
        except (RoombaError, TimeoutError, OSError):
            raise KitError(*await self._unreachable(), self.robot_id) from None
        self._client = client
        self._notify(True, None)

    async def disconnect(self) -> None:
        client, self._client = self._client, None
        for task in list(self._tasks):
            task.cancel()
        if client is not None:
            await client.disconnect()

    def on_reported(self, callback: ReportedCallback) -> None:
        self._reported_cbs.append(callback)

    def on_connection(self, callback: ConnectionCallback) -> None:
        self._connection_cbs.append(callback)

    async def send_command(self, name: str, params: dict[str, Any] | None = None) -> None:
        client = self._connected()
        try:
            await client.send_command(name, params)
        except RoombaScopeError:
            raise KitError(
                ErrorCode.INVALID_REQUEST,
                f"`{name}` with an empty room list would clean the whole home.",
                self.robot_id,
            ) from None
        except RoombaError:
            raise self._offline() from None

    async def publish_settings(self, values: dict[str, Any]) -> None:
        # set_preferences sends every key in one `delta` {"state": {...}}; nested dicts pass as-is.
        client = self._connected()
        try:
            await client.set_preferences(values)
        except RoombaError:
            raise self._offline() from None

    async def request_position(self, timeout: float) -> PositionFix | None:
        client = self._connected()
        try:
            pose = await client.get_position(timeout=timeout)
        except (TimeoutError, RrtpUnsupportedError):
            raise TimeoutError from None
        except RoombaError:
            raise self._offline() from None
        return None if pose is None else to_position_fix(self.robot_id, pose)

    # -- internals -------------------------------------------------------------------------------

    def _connected(self) -> RoombaClient:
        if self._client is None:
            raise self._offline()
        return self._client

    def _offline(self) -> KitError:
        return KitError(ErrorCode.ROBOT_OFFLINE, "Not connected to the robot.", self.robot_id)

    async def _unreachable(self) -> tuple[ErrorCode, str]:
        """Classify a failed or lost session: does the robot still answer discovery?"""
        robot = await discovery.probe(self.address, timeout_s=HELD_PROBE_TIMEOUT_S)
        if robot is not None and robot.id == self.robot_id:
            return (
                ErrorCode.CONNECTION_HELD_ELSEWHERE,
                "The robot is online but another client holds its local connection.",
            )
        return ErrorCode.ROBOT_OFFLINE, f"The robot at {self.address} is not reachable."

    def _notify(self, connected: bool, reason: str | None) -> None:
        for callback in list(self._connection_cbs):
            callback(connected, reason)

    def _on_message(self, message: dict[str, Any]) -> None:
        state = message.get("state")
        reported = state.get("reported") if isinstance(state, dict) else None
        if isinstance(reported, dict) and reported:
            for callback in list(self._reported_cbs):
                callback(reported)

    def _on_state(self, client: RoombaClient, state: str) -> None:
        # Runs inline in roombapy's supervisor task, so the teardown (which cancels that task)
        # has to run as a task of its own.
        if state == "disconnected" and client is self._client:
            self._client = None
            self._spawn(self._lost(client))

    async def _lost(self, client: RoombaClient) -> None:
        auth_failed = client.auth_error is not None
        await client.disconnect()
        # A dropped session isn't evidence of another client: robots drop connections on their
        # own (HW-1 saw an i5 do it twice, mislabelled held_elsewhere). If another client
        # really holds it, the reconnect is refused and `connect()` classifies that.
        code = ErrorCode.AUTH_FAILED if auth_failed else ErrorCode.ROBOT_OFFLINE
        log.info("robot %s link lost: %s", self.robot_id, code.value)
        self._notify(False, code.value)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class LocalPasswordRetriever:
    """A `PasswordRetriever` using the dock's listening mode (HOME held ~2 s until it chimes)."""

    async def retrieve(self, address: str, timeout: float) -> str:
        try:
            password = await RoombaPassword(address).get_password(timeout)
        except (UnicodeDecodeError, OSError):
            password = None
        if not password:
            # roombapy returns None for silence, refusal and the cloud-only magic alike.
            raise KitError(
                ErrorCode.PASSWORD_UNAVAILABLE,
                "The robot gave no password. Put it on its dock, hold HOME about 2 s until it "
                "chimes, then retry within 30 s. Some models only serve it from the cloud.",
            )
        return password
