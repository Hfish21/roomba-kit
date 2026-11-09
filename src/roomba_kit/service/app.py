"""The composition root: `ServiceContext` owns settings, secrets, storage, events and sessions.

It satisfies `core.registry.OperationContext`, so every operation handler receives it.
"""

import asyncio
import contextlib
import itertools
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import datetime
from typing import Literal, Protocol

from roomba_kit import __version__
from roomba_kit.config import settings as config
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.redact import RedactFilter, install_redaction
from roomba_kit.config.settings import RobotEntry, Settings
from roomba_kit.config.tokens import TokenStore
from roomba_kit.core.capabilities import Capability, Verification
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import DiscoveredRobot, KitModel
from roomba_kit.profiles.base import ModelProfile
from roomba_kit.profiles.loader import load_profiles, match_profile
from roomba_kit.service.components import Stop, load_components
from roomba_kit.service.eventbus import EventBus
from roomba_kit.service.session import RobotSession, StateListener
from roomba_kit.service.store import Store
from roomba_kit.transport import discovery
from roomba_kit.transport.base import (
    CloudTransport,
    CloudTransportFactory,
    PasswordRetriever,
    RobotTransportFactory,
)

log = logging.getLogger(__name__)

Discover = Callable[[str, float], Awaitable[list[DiscoveredRobot]]]
Probe = Callable[[str, float], Awaitable[DiscoveredRobot | None]]


class CloudStatus(KitModel):
    status: Literal["connected", "unavailable", "not_configured"]
    reason: str | None = None
    last_ok: datetime | None = None


class CloudService(Protocol):
    """The optional cloud subsystem. US1's CloudManager replaces `NoCloud` as a component."""

    def status(self) -> CloudStatus: ...

    def transport(self) -> CloudTransport:
        """The logged-in cloud transport, or raises `cloud_unavailable` with a reason."""
        ...


class NoCloud:
    def status(self) -> CloudStatus:
        return CloudStatus(status="not_configured")

    def transport(self) -> CloudTransport:
        raise KitError(
            ErrorCode.CLOUD_UNAVAILABLE,
            "No iRobot cloud credentials are configured.",
            details={"reason": "not_configured"},
        )


def _default_transport_factory() -> RobotTransportFactory:
    from roomba_kit.transport.local import LocalTransport

    return LocalTransport


def _default_password_retriever() -> PasswordRetriever:
    from roomba_kit.transport.local import LocalPasswordRetriever

    return LocalPasswordRetriever()


def _default_cloud_factory() -> CloudTransportFactory:
    from roomba_kit.transport.cloud import IRobotCloud

    return IRobotCloud


def _match(sku: str) -> str | None:
    profile = match_profile(sku)
    return profile.id if profile else None


async def _discover(broadcast: str, timeout_s: float) -> list[DiscoveredRobot]:
    return await discovery.discover(broadcast, timeout_s, match=_match)


async def _probe(address: str, timeout_s: float) -> DiscoveredRobot | None:
    return await discovery.probe(address, timeout_s, match=_match)


class ServiceContext:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        transport_factory: RobotTransportFactory | None = None,
        password_retriever: PasswordRetriever | None = None,
        cloud_factory: CloudTransportFactory | None = None,
        discover: Discover | None = None,
        probe: Probe | None = None,
        session_options: dict[str, float] | None = None,
    ) -> None:
        self.settings = settings or config.load_settings()
        self.transport_factory = transport_factory or _default_transport_factory()
        self.password_retriever = password_retriever or _default_password_retriever()
        self.cloud_factory = cloud_factory or _default_cloud_factory()
        self._discover = discover or _discover
        self._probe = probe or _probe
        self._session_options = session_options or {}
        self.credentials = CredentialStore()
        self.tokens = TokenStore(self.credentials)
        self.profiles: dict[str, ModelProfile] = {}
        self.store = Store()
        self.bus = EventBus(self.store, persist_position=self.settings.events.persist_position)
        self.sessions: dict[str, RobotSession] = {}
        self.cloud: CloudService = NoCloud()
        self.state_listeners: list[StateListener] = []
        self.started_at = time.monotonic()
        self._stops: list[Stop] = []
        self._pruner: asyncio.Task[None] | None = None
        self._redactor: RedactFilter | None = None
        self.running = False

    # -- OperationContext ------------------------------------------------------------------------

    @property
    def version(self) -> str:
        return __version__

    @property
    def uptime_s(self) -> int:
        return int(time.monotonic() - self.started_at)

    # -- lifecycle -------------------------------------------------------------------------------

    async def start(self) -> None:
        """Start everything, or raise without leaving anything running (FR-013, SC-009)."""
        self.credentials.refresh(force=True)  # refuses a credentials file broader than 0600
        self._redactor = install_redaction(self.secrets)
        self.profiles = load_profiles()
        await self.store.open()
        await self.bus.start()
        for entry in self.settings.robots:
            self.sessions[entry.id] = self._new_session(entry)
        for session in self.sessions.values():
            await session.start()
        for name, start in load_components().items():
            try:
                stop = await start(self)
            except Exception:
                log.exception("service component %s failed to start", name)
                await self.stop()
                raise
            if stop is not None:
                self._stops.append(stop)
        self._pruner = self.store.start_pruner(lambda: self.settings.history.event_retention_days)
        self.started_at = time.monotonic()
        self.running = True

    async def stop(self) -> None:
        self.running = False
        for stop in reversed(self._stops):
            try:
                await stop()
            except Exception:
                log.exception("service component failed to stop")
        self._stops = []
        for session in self.sessions.values():
            await session.stop()
        if self._pruner is not None:
            self._pruner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pruner
            self._pruner = None
        with contextlib.suppress(Exception):
            self.tokens.flush()
        with contextlib.suppress(RuntimeError):
            await self.bus.close()
        await self.store.close()

    def secrets(self) -> list[str]:
        """Every secret currently known, for the log redactor. Never raises."""
        try:
            return list(itertools.chain(self.credentials.secrets(), self.tokens.secrets()))
        except KitError:
            return list(self.tokens.secrets())

    # -- robots ----------------------------------------------------------------------------------

    def profile_for(self, entry: RobotEntry) -> ModelProfile | None:
        if entry.profile and entry.profile in self.profiles:
            return self.profiles[entry.profile]
        return match_profile(entry.sku) if entry.sku else None

    def _password_for(self, robot_id: str) -> Callable[[], str | None]:
        def provider() -> str | None:
            try:
                return self.credentials.robot_password(robot_id)
            except KitError:
                log.error("credentials file is unreadable (check its permissions)")
                return None

        return provider

    def _new_session(self, entry: RobotEntry) -> RobotSession:
        return RobotSession(
            entry,
            self.profile_for(entry),
            self.transport_factory,
            self._password_for(entry.id),
            self.bus,
            listeners=self.state_listeners,
            rediscover=self.rediscover,
            on_address=self._save_address,
            **self._session_options,
        )

    def add_state_listener(self, listener: StateListener) -> Callable[[], None]:
        self.state_listeners.append(listener)
        return lambda: self.state_listeners.remove(listener)

    def session(self, robot_id: str) -> RobotSession:
        try:
            return self.sessions[robot_id]
        except KeyError:
            raise KitError(
                ErrorCode.ROBOT_NOT_FOUND, f"No configured robot with ID {robot_id}."
            ) from None

    def resolve_robot(self, ref: str) -> RobotSession:
        """A robot by its ID, or by an unambiguous display name (case-insensitive, FR-020)."""
        if ref in self.sessions:
            return self.sessions[ref]
        by_id = [s for s in self.sessions.values() if s.robot_id.lower() == ref.lower()]
        if by_id:
            return by_id[0]
        named = [s for s in self.sessions.values() if s.robot().name.lower() == ref.lower()]
        if len(named) == 1:
            return named[0]
        if named:
            raise KitError(
                ErrorCode.AMBIGUOUS_NAME,
                f"{len(named)} robots are named {ref!r}; use a robot ID.",
                details={"candidates": [{"id": s.robot_id, "name": s.name} for s in named]},
            )
        raise KitError(
            ErrorCode.ROBOT_NOT_FOUND,
            f"No configured robot matches {ref!r}.",
            details={"valid": [{"id": s.robot_id, "name": s.robot().name} for s in self]},
        )

    def __iter__(self) -> Iterator[RobotSession]:
        return iter(sorted(self.sessions.values(), key=lambda s: (s.name.lower(), s.robot_id)))

    async def add_robot(self, entry: RobotEntry, password: str | None = None) -> RobotSession:
        """Persist a robot (and optionally its password) and start or refresh its session."""
        if password:
            self.credentials.set_robot_password(entry.id, password)
        existing = config.load_config().robot(entry.id)
        if existing is not None:
            entry = existing.model_copy(
                update={k: v for k, v in entry.model_dump().items() if v not in (None, "")}
            )
        config.upsert_robot(entry)
        self._replace_settings_robot(entry)
        if entry.id in self.sessions:
            session = self.sessions[entry.id]
            if entry.address and entry.address != session.address:
                session.address = entry.address
            session.credentials_changed()
            return session
        session = self._new_session(entry)
        self.sessions[entry.id] = session
        if self.running:
            await session.start()
        return session

    async def set_robot_password(self, robot_id: str, password: str) -> None:
        self.credentials.set_robot_password(robot_id, password)
        self.session(robot_id).credentials_changed()

    async def remove_robot(self, robot_id: str) -> bool:
        session = self.sessions.pop(robot_id, None)
        if session is not None:
            await session.stop()
        removed_config = config.remove_robot(robot_id)
        removed_creds = self.credentials.remove_robot(robot_id)
        self.settings.robots = [r for r in self.settings.robots if r.id != robot_id]
        return session is not None or removed_config or removed_creds

    def _replace_settings_robot(self, entry: RobotEntry) -> None:
        others = [r for r in self.settings.robots if r.id != entry.id]
        self.settings.robots = [*others, entry]

    def _save_address(self, robot_id: str, address: str) -> None:
        entry = config.load_config().robot(robot_id)
        if entry is None:
            return
        updated = entry.model_copy(update={"address": address})
        config.upsert_robot(updated)
        self._replace_settings_robot(updated)

    async def discover(
        self, broadcast: str | None = None, timeout_s: float = 8.0
    ) -> list[DiscoveredRobot]:
        found = await self._discover(broadcast or self.settings.discovery.broadcast, timeout_s)
        return [r.model_copy(update={"configured": r.id in self.sessions}) for r in found]

    async def probe(self, address: str, timeout_s: float = 3.0) -> DiscoveredRobot | None:
        robot = await self._probe(address, timeout_s)
        return robot.model_copy(update={"configured": robot.id in self.sessions}) if robot else None

    async def rediscover(self, robot_id: str) -> str | None:
        for robot in await self._discover(self.settings.discovery.broadcast, 8.0):
            if robot.id == robot_id:
                return robot.address
        return None

    # -- capabilities ----------------------------------------------------------------------------

    def capability_support(self, capability: Capability) -> dict[str, Verification]:
        """Which installed profiles declare `capability`, with their verification status."""
        return {
            pid: profile.capabilities[capability]
            for pid, profile in sorted(self.profiles.items())
            if capability in profile.capabilities
        }
