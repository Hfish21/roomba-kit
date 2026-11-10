"""Setup operations: discovery, adding and removing robots, and their passwords
(contracts/operations.md, "System & setup"; FR-001–FR-006).

Every robot password is verified by connecting before it's kept (FR-006). The check runs
through the robot's own session, not a second connection: robots accept one local client, so a
side probe would evict the session or be refused (Constitution II). The password is written,
the session reconnects with it, and the previous password is put back unless it connects.
"""

import asyncio
import ipaddress
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from roomba_kit.config.settings import RobotEntry
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import Event, EventType
from roomba_kit.core.models import ConnectionState, DiscoveredRobot, KitModel
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.profiles.base import DEFAULT_PAIRING_STEPS, ModelProfile
from roomba_kit.profiles.loader import get_profile
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.session import RobotSession

VERIFY_TIMEOUT_S = 20.0
"""How long a new password gets to connect before the robot counts as unreachable."""
SETTLE_S = 0.25
"""A connect result counts once no new attempt follows it within this long (see `_attempt`)."""

RETRY_S = 2.0
"""Pause between local password retrieval attempts."""
ATTEMPT_S = 10.0
"""The longest one retrieval attempt may wait for the robot."""

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


class Discover(BaseModel):
    broadcast: str | None = Field(
        default=None,
        description="Where to send the discovery datagram: your subnet's broadcast address "
        "(e.g. 192.0.2.255) when a VPN owns the default route, or one robot's IP. "
        "Default: `[discovery] broadcast` in config.toml",
    )
    timeout_s: float = Field(default=8.0, gt=0, le=60, description="How long to listen")

    @field_validator("broadcast")
    @classmethod
    def _address(cls, value: str | None) -> str | None:
        if value is not None:
            ipaddress.ip_address(value)
        return value


@operation(
    description="Find iRobot robots on the local network: ID, name, SKU, firmware, address, "
    "whether the model is supported, and whether it's already configured.",
    params=Discover,
    result=list[DiscoveredRobot],
    read_only=True,
    http=Http("GET", "/discover_robots"),
    cli=Cli((), "discover"),
)
async def discover_robots(ctx: ServiceContext, params: Discover) -> list[DiscoveredRobot]:
    return await ctx.discover(params.broadcast, params.timeout_s)


class Address(BaseModel):
    address: str = Field(description="The robot's IP address")

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        ipaddress.ip_address(value)
        return value


@operation(
    description="Add the robot at an IP address, for when discovery can't reach it. The robot "
    "must answer there. Next, give it a password with `retrieve_password`.",
    params=Address,
    result=DiscoveredRobot,
    http=Http("POST", "/robots"),
    cli=Cli(("robots",), "add", args=("address",)),
)
async def add_robot(ctx: ServiceContext, params: Address) -> DiscoveredRobot:
    found = await ctx.probe(params.address)
    if found is None:
        raise KitError(
            ErrorCode.ROBOT_OFFLINE,
            f"No robot answered at {params.address}. Check the address and that the robot is "
            "on the same network.",
        )
    entry = RobotEntry(
        id=found.id,
        name=found.name,
        address=found.address,
        profile=found.profile_id,
        sku=found.sku,
        firmware=found.firmware,
    )
    await ctx.add_robot(entry)
    return found.model_copy(update={"configured": True})


def pairing_steps(profile: ModelProfile | None) -> list[str]:
    """The profile's pairing steps; the shared default for an unsupported model."""
    return list(profile.pairing_steps if profile else DEFAULT_PAIRING_STEPS)


def pairing_steps_for(profile_id: str | None) -> list[str]:
    """`pairing_steps` by profile ID, for clients that only have the robot's `profile_id`."""
    return pairing_steps(get_profile(profile_id) if profile_id else None)


class RetrieveParams(BaseModel):
    robot: str = Field(description="Robot ID or unambiguous name")
    timeout_s: float = Field(
        default=60.0, gt=0, le=600, description="How long to keep retrying (every 2 s)"
    )


class PasswordRetrieval(KitModel):
    outcome: Literal["stored", "timeout", "cloud_only"] = Field(
        description="`stored`: retrieved, verified and saved. `timeout`: the robot never "
        "answered. `cloud_only`: it answered but didn't release a password; retry with HOME "
        "held, or use `cloud_retrieve_password`."
    )
    steps: list[str] = Field(description="What to do at the robot")


@operation(
    description="Retrieve the robot's local password over the network while you hold HOME on "
    "the docked robot, verify it by connecting, and store it. The password is never returned.",
    params=RetrieveParams,
    result=PasswordRetrieval,
    http=Http("POST", "/robots/{robot}/password/retrieve"),
    cli=Cli(("robots",), "password", args=("robot",)),
)
async def retrieve_password(ctx: ServiceContext, params: RetrieveParams) -> PasswordRetrieval:
    session = ctx.resolve_robot(params.robot)
    if not session.address:
        code, message = _UNREACHABLE[ConnectionState.NOT_CONFIGURED]
        raise KitError(code, message, session.robot_id)
    steps = pairing_steps(session.profile)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + params.timeout_s
    refused = False
    while (remaining := deadline - loop.time()) > 0:
        try:
            password = await ctx.password_retriever.retrieve(
                session.address, min(remaining, ATTEMPT_S)
            )
        except KitError as err:
            # The robot says nothing until HOME is held, so keep trying for the whole window.
            # Only an actual answer without a password counts as a refusal (`cloud_only`).
            if err.code is ErrorCode.PASSWORD_UNAVAILABLE:
                refused = True
            await asyncio.sleep(min(RETRY_S, max(deadline - loop.time(), 0)))
            continue
        if await store_verified_password(ctx, session, password) is ConnectionState.AUTH_FAILED:
            raise KitError(
                ErrorCode.AUTH_FAILED,
                "The robot released a password but then rejected it; nothing was saved. Retry.",
                session.robot_id,
            )
        return PasswordRetrieval(outcome="stored", steps=steps)
    if refused:
        return PasswordRetrieval(outcome="cloud_only", steps=steps)
    return PasswordRetrieval(outcome="timeout", steps=steps)


class RobotRef(BaseModel):
    robot: str = Field(description="Robot ID or unambiguous name")


class Removed(KitModel):
    removed: bool


@operation(
    description="Remove a robot: stop its connection and delete its config and password. "
    "Other robots and the cloud login are untouched.",
    params=RobotRef,
    result=Removed,
    http=Http("DELETE", "/robots/{robot}"),
    cli=Cli(("robots",), "remove", args=("robot",)),
)
async def remove_robot(ctx: ServiceContext, params: RobotRef) -> Removed:
    try:
        robot_id = ctx.resolve_robot(params.robot).robot_id
    except KitError as err:
        if err.code is not ErrorCode.ROBOT_NOT_FOUND:
            raise
        robot_id = params.robot  # leftovers in the files are still cleaned up
    return Removed(removed=await ctx.remove_robot(robot_id))
