"""Setup operations: robot passwords (contracts/operations.md, "System & setup").

Every robot password is verified by connecting before it's kept (FR-006). The check runs
through the robot's own session, not a second connection: robots accept one local client, so a
side probe would evict the session or be refused (Constitution II). The password is written,
the session reconnects with it, and the previous password is put back unless it connects.
"""

import asyncio
from typing import Literal

from pydantic import BaseModel, Field

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import Event, EventType
from roomba_kit.core.models import ConnectionState, KitModel
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.session import RobotSession

VERIFY_TIMEOUT_S = 20.0
"""How long a new password gets to connect before the robot counts as unreachable."""
SETTLE_S = 0.25
"""A connect result counts once no new attempt follows it within this long (see `_attempt`)."""

_locks: dict[str, asyncio.Lock] = {}

_UNREACHABLE = {
    ConnectionState.OFFLINE: (
        ErrorCode.ROBOT_OFFLINE,
        "Couldn't reach the robot to check the password, so it wasn't saved.",
    ),
    ConnectionState.HELD_ELSEWHERE: (
        ErrorCode.CONNECTION_HELD_ELSEWHERE,
        "Another client (the iRobot app?) holds the robot's connection, so the password couldn't "
        "be checked and wasn't saved. Close it and retry.",
    ),
    ConnectionState.NOT_CONFIGURED: (
        ErrorCode.ROBOT_OFFLINE,
        "The robot has no known address, so the password couldn't be checked. "
        "Re-add it with `roomba-kit robots add ADDRESS`.",
    ),
}


async def _attempt(events: "asyncio.Queue[ConnectionState]") -> ConnectionState:
    """The outcome of the session's first connection attempt that starts after the write.

    An attempt already under way when the password was written used the old one; the session
    follows it with a fresh attempt at once (credentials changed), so a `connected` or
    `auth_failed` only counts when no new `connecting` follows within `SETTLE_S`.
    """
    started = False
    candidate: ConnectionState | None = None
    while True:
        try:
            async with asyncio.timeout(SETTLE_S if candidate else None):
                to = await events.get()
        except TimeoutError:
            assert candidate is not None  # noqa: S101 - only a settle wait can time out here
            return candidate
        if to is ConnectionState.CONNECTING:
            started, candidate = True, None
        elif not started:
            continue
        elif to in (ConnectionState.CONNECTED, ConnectionState.AUTH_FAILED):
            candidate = to
        else:
            return to


def _write(ctx: ServiceContext, robot_id: str, password: str | None) -> str | None:
    """Set (or, with None, remove) the robot's password in the file; return the file's old one."""
    with ctx.credentials.edit() as data:
        robots = data.setdefault("robots", {})
        entry = robots.setdefault(robot_id, {})
        previous = entry.get("password")
        if password:
            entry["password"] = password
        else:
            entry.pop("password", None)
            if not entry:
                robots.pop(robot_id)
    return previous if isinstance(previous, str) and previous else None


async def store_verified_password(
    ctx: ServiceContext, session: RobotSession, password: str
) -> Literal[ConnectionState.CONNECTED, ConnectionState.AUTH_FAILED]:
    """Keep `password` for the robot only if its session connects with it (FR-006).

    Returns `CONNECTED` (kept) or `AUTH_FAILED` (rejected; the previous password is restored).
    Raises `robot_offline` / `connection_held_elsewhere` when it can't be checked, after
    restoring the previous password, and `invalid_request` when an env var overrides it.
    """
    robot_id = session.robot_id
    async with _locks.setdefault(robot_id, asyncio.Lock()):
        if not session.address:
            code, message = _UNREACHABLE[ConnectionState.NOT_CONFIGURED]
            raise KitError(code, message, robot_id)
        current = ctx.credentials.robot_password(robot_id)
        if current == password and session.connection is ConnectionState.CONNECTED:
            return ConnectionState.CONNECTED

        events: asyncio.Queue[ConnectionState] = asyncio.Queue()

        async def on_event(event: Event) -> None:
            events.put_nowait(ConnectionState(event.data["to"]))

        unsubscribe = ctx.bus.subscribe(on_event, [EventType.CONNECTION_CHANGED], robot_id)
        previous = _write(ctx, robot_id, password)
        session.credentials_changed()
        keep = False
        try:
            if ctx.credentials.robot_password(robot_id) != password:
                raise KitError(
                    ErrorCode.INVALID_REQUEST,
                    "This robot's password is set by the ROOMBA_KIT_ROBOTS__<ID>__PASSWORD "
                    "environment variable, which overrides the stored one. Change it there.",
                    robot_id,
                )
            try:
                async with asyncio.timeout(VERIFY_TIMEOUT_S):
                    outcome = await _attempt(events)
            except TimeoutError:
                outcome = ConnectionState.OFFLINE
            keep = outcome is ConnectionState.CONNECTED
        finally:
            unsubscribe()
            if not keep:
                _write(ctx, robot_id, previous)
                session.credentials_changed()
        if outcome is ConnectionState.CONNECTED or outcome is ConnectionState.AUTH_FAILED:
            return outcome
        code, message = _UNREACHABLE.get(outcome, _UNREACHABLE[ConnectionState.OFFLINE])
        raise KitError(code, message, robot_id)


class RobotPassword(BaseModel):
    robot: str = Field(description="Robot ID or unambiguous name")
    password: str = Field(
        min_length=1,
        description="The robot's local password. The CLI prompts for it; it is never echoed, "
        "returned or logged.",
    )


class PasswordStored(KitModel):
    outcome: Literal["stored", "auth_failed"]


@operation(
    description="Store a robot's local password you already know, after checking that the "
    "robot accepts it. Nothing is saved if the robot rejects it or can't be reached.",
    params=RobotPassword,
    result=PasswordStored,
    http=Http("POST", "/robots/{robot}/password"),
    cli=Cli(("robots",), "set-password", args=("robot",), secret=("password",)),
    surfaces={"http", "cli"},
)
async def set_robot_password(ctx: ServiceContext, params: RobotPassword) -> PasswordStored:
    session = ctx.resolve_robot(params.robot)
    if await store_verified_password(ctx, session, params.password) is ConnectionState.AUTH_FAILED:
        return PasswordStored(outcome="auth_failed")
    return PasswordStored(outcome="stored")
