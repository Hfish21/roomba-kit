"""CloudManager: the optional iRobot cloud subsystem (research R12, Constitution I).

One shared login for the account, made in the background. Its status is `not_configured`,
`connected` or `unavailable(reason)`. Failures back off exponentially up to 15 minutes; rejected
credentials are not retried until they change (an account can lock after repeated bad logins).
Every status change is published as a `cloud.status` event.

Nothing here is ever awaited on a local path: `status()` and `transport()` are synchronous and
answer from memory, so a dead cloud can't block or slow robot control.
"""

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from roomba_kit.config import settings as config
from roomba_kit.config.credentials import CloudCredential
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import CloudStatusData, EventType
from roomba_kit.service.app import CloudStatus
from roomba_kit.service.components import Stop, component
from roomba_kit.transport.base import CloudTransport

if TYPE_CHECKING:
    from roomba_kit.service.app import ServiceContext

log = logging.getLogger(__name__)

BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 15 * 60.0
LOGIN_TIMEOUT_S = 30.0
POLL_S = 30.0
"""How often an idle or connected manager re-checks credentials for out-of-band edits."""

DISABLED = "disabled"
NOT_CONFIGURED = "not_configured"
AUTH_FAILED = ErrorCode.AUTH_FAILED.value
UNREADABLE = "credentials_unreadable"
CONNECTING = "connecting"
DATA_REASONS = frozenset(
    {
        "not_yet_published",
        "request_refused",
        "response_malformed",
        "rate_limited",
        "no_robots",
        "robot_ambiguous",
    }
)
"""`cloud_unavailable` reasons about one request, not the login: they don't trigger a re-login."""


def _reason(err: KitError) -> str:
    if err.code is ErrorCode.AUTH_FAILED:
        return AUTH_FAILED
    reason = (err.details or {}).get("reason")
    return str(reason) if reason else err.code.value


def _key(cred: CloudCredential, country: str) -> str:
    """A fingerprint of the login, so the manager can tell when credentials change."""
    raw = "\0".join((cred.username, cred.password, country)).encode()
    return hashlib.sha256(raw).hexdigest()


async def _close(transport: CloudTransport) -> None:
    with contextlib.suppress(Exception):
        await transport.close()


async def _login(transport: CloudTransport) -> None:
    """Log in, mapping a hang or a non-kit exception to `cloud_unavailable`."""
    try:
        async with asyncio.timeout(LOGIN_TIMEOUT_S):
            await transport.login()
    except KitError:
        raise
    except TimeoutError:
        raise KitError(
            ErrorCode.CLOUD_UNAVAILABLE,
            "iRobot cloud login timed out.",
            details={"reason": "timeout"},
        ) from None
    except Exception as exc:
        log.warning("cloud login failed unexpectedly (%s)", type(exc).__name__)
        raise KitError(
            ErrorCode.CLOUD_UNAVAILABLE,
            "iRobot cloud login failed.",
            details={"reason": "connection_failed"},
        ) from None


class CloudManager:
    """A `CloudService` that keeps one logged-in `CloudTransport` for the account."""

    def __init__(
        self,
        ctx: "ServiceContext",
        *,
        backoff_min: float | None = None,
        backoff_max: float | None = None,
        poll_s: float | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._ctx = ctx
        self._backoff_min = BACKOFF_MIN_S if backoff_min is None else backoff_min
        self._backoff_max = BACKOFF_MAX_S if backoff_max is None else backoff_max
        self._poll_s = POLL_S if poll_s is None else poll_s
        self._clock = clock
        self._status = CloudStatus(status="not_configured")
        self._transport: CloudTransport | None = None
        self._transport_key: str | None = None
        self._stale = False
        self._rejected: str | None = None
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.backoff_history: list[float] = []
        """Every backoff delay slept, in order (diagnostics and tests)."""

    def __repr__(self) -> str:
        return f"CloudManager({self._status.status!r}, reason={self._status.reason!r})"

    # -- CloudService ----------------------------------------------------------------------------

    def status(self) -> CloudStatus:
        return self._status

    def transport(self) -> CloudTransport:
        """The logged-in transport, or `cloud_unavailable` with `details.reason`. Never waits."""
        if self._status.status == "connected" and self._transport is not None:
            return self._transport
        reason = self._status.reason or self._status.status
        message = {
            DISABLED: "The iRobot cloud is disabled in config.toml ([cloud] enabled = false).",
            NOT_CONFIGURED: "No iRobot cloud credentials are configured "
            "(add them with `roomba-kit cloud login`).",
            AUTH_FAILED: "The iRobot cloud rejected the stored account login.",
        }.get(reason, f"The iRobot cloud is unavailable ({reason}).")
        raise KitError(ErrorCode.CLOUD_UNAVAILABLE, message, details={"reason": reason})

    # -- for operations and other subsystems -----------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._ctx.settings.cloud.enabled

    async def robot_password(self, blid: str) -> str | None:
        """The robot's local password from the account, or None if the account doesn't list it."""
        transport = self.transport()
        try:
            return await transport.robot_password(blid)
        except KitError as err:
            self.report_failure(err)
            raise

    def report_failure(self, err: KitError) -> None:
        """A cloud call failed: re-check the login in the background (status may then change).

        Data-level failures (`not_yet_published`, a missing map) don't: the login is fine.
        """
        if err.code is ErrorCode.AUTH_FAILED or (
            err.code is ErrorCode.CLOUD_UNAVAILABLE and _reason(err) not in DATA_REASONS
        ):
            self._wake.set()

    def refresh(self) -> None:
        """Re-read credentials and log in again now, skipping any backoff wait.

        Credentials the cloud rejected stay rejected until they change.
        """
        self._wake.set()

    async def configure(self, username: str, password: str, country: str) -> CloudStatus:
        """Log in with new credentials and save them only if the login succeeds (FR-006).

        Raises `auth_failed` or `cloud_unavailable` without touching the saved credentials or
        the current login. On success the validated login is adopted, so it isn't repeated.
        """
        if not self.enabled:
            raise KitError(
                ErrorCode.CLOUD_UNAVAILABLE,
                "The iRobot cloud is disabled in config.toml ([cloud] enabled = false).",
                details={"reason": DISABLED},
            )
        transport = self._ctx.cloud_factory(username, password, country)
        try:
            await _login(transport)
        except BaseException:
            await _close(transport)
            raise
        async with self._lock:
            await self._halt()
            self._ctx.credentials.set_cloud(username, password)
            self._save_country(country)
            self._rejected = None
            self._transport = transport
            self._transport_key = _key(CloudCredential(username, password), country)
            await self._set("connected", last_ok=self._clock())
            self._spawn()
        return self._status

    async def clear(self) -> CloudStatus:
        """Forget the saved credentials and log out. Env-supplied credentials still apply."""
        async with self._lock:
            await self._halt()
            self._ctx.credentials.clear_cloud()
            self._rejected = None
            cred, missing = self._credential()
            if cred is None:
                await self._set(*self._missing_status(missing))
            self._spawn()
        return self._status

    # -- lifecycle -------------------------------------------------------------------------------

    async def start(self) -> None:
        self._spawn()

    async def stop(self) -> None:
        async with self._lock:
            await self._halt()

    def _spawn(self) -> None:
        self._wake.clear()
        self._task = asyncio.get_running_loop().create_task(self._supervise(), name="cloud")

    async def _halt(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._drop()

    async def _drop(self) -> None:
        transport, self._transport, self._transport_key = self._transport, None, None
        if transport is not None:
            await _close(transport)

    # -- supervision -----------------------------------------------------------------------------

    def _save_country(self, country: str) -> None:
        if self._ctx.settings.cloud.country == country:
            return
        file = config.load_config()
        file.cloud.country = country
        config.save_config(file)
        self._ctx.settings.cloud.country = country

    def _credential(self) -> tuple[CloudCredential | None, str | None]:
        """(credential, reason it's missing)."""
        if not self.enabled:
            return None, DISABLED
        try:
            cred = self._ctx.credentials.cloud()
        except KitError:
            return None, UNREADABLE
        return cred, None if cred else NOT_CONFIGURED

    async def _set(
        self, status: Any, reason: str | None = None, last_ok: datetime | None = None
    ) -> None:
        previous = self._status
        self._status = CloudStatus(
            status=status, reason=reason, last_ok=last_ok or previous.last_ok
        )
        if (previous.status, previous.reason) != (status, reason):
            log.info("cloud: %s -> %s (%s)", previous.status, status, reason or "-")
            await self._ctx.bus.publish(
                EventType.CLOUD_STATUS, None, CloudStatusData(status=status, reason=reason)
            )

    async def _sleep(self, delay: float) -> bool:
        """Wait `delay` seconds or until woken. True when woken."""
        try:
            await asyncio.wait_for(self._wake.wait(), delay)
        except TimeoutError:
            return False
        self._wake.clear()
        return True

    async def _idle_until_changed(self, key: str | None) -> None:
        """Sleep until woken, or until the credentials on disk/env no longer match `key`."""
        while not await self._sleep(self._poll_s):
            cred, _ = self._credential()
            current = _key(cred, self._ctx.settings.cloud.country) if cred else None
            if current != key:
                return

    @staticmethod
    def _missing_status(missing: str | None) -> tuple[str, str | None]:
        if missing == UNREADABLE:
            return "unavailable", missing
        return "not_configured", None if missing == NOT_CONFIGURED else missing

    async def _supervise(self) -> None:
        backoff = self._backoff_min
        while True:
            cred, missing = self._credential()
            if cred is None:
                await self._drop()
                await self._set(*self._missing_status(missing))
                await self._idle_until_changed(None)
                continue
            country = self._ctx.settings.cloud.country
            key = _key(cred, country)
            if key == self._rejected:
                await self._drop()
                await self._set("unavailable", AUTH_FAILED)
                await self._idle_until_changed(key)
                continue
            if self._transport is None or self._transport_key != key or self._stale:
                # Keep serving the current login (if any) until its replacement is ready.
                if self._status.status == "not_configured":
                    await self._set("unavailable", CONNECTING)
                transport = self._ctx.cloud_factory(cred.username, cred.password, country)
                try:
                    await _login(transport)
                except KitError as err:
                    await _close(transport)
                    await self._drop()
                    if err.code is ErrorCode.AUTH_FAILED:
                        self._rejected = key
                        continue
                    await self._set("unavailable", _reason(err))
                    delay, backoff = backoff, min(backoff * 2, self._backoff_max)
                    self.backoff_history.append(delay)
                    await self._sleep(delay)
                    continue
                old, self._transport, self._transport_key = self._transport, transport, key
                if old is not None:
                    await _close(old)
                await self._set("connected", last_ok=self._clock())
            backoff, self._stale = self._backoff_min, False
            await self._idle_until_changed(key)
            self._stale = True


def manager(ctx: Any) -> CloudManager:
    """The running CloudManager, or `cloud_unavailable` when the service hasn't started one."""
    cloud = getattr(ctx, "cloud", None)
    if not isinstance(cloud, CloudManager):
        raise KitError(
            ErrorCode.CLOUD_UNAVAILABLE,
            "The cloud subsystem isn't running.",
            details={"reason": "not_started"},
        )
    return cloud


@component("cloud")
async def start(ctx: "ServiceContext") -> Stop:
    cloud = CloudManager(ctx)
    ctx.cloud = cloud
    await cloud.start()
    return cloud.stop
