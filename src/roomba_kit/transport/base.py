"""Kit-owned transport interfaces. Adapters raise only `KitError`, never library exceptions.

Error codes a transport may raise: `robot_offline`, `connection_held_elsewhere`,
`auth_failed`, `password_unavailable` (and `cloud_unavailable` / `timeout` for the cloud).
"""

from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from roomba_kit.core.models import PositionFix

ReportedCallback = Callable[[dict[str, Any]], None]
"""Called with each `state.reported` fragment as it arrives."""

ConnectionCallback = Callable[[bool, str | None], None]
"""Called with `(connected, reason)` whenever the link comes up or drops."""


class RobotTransport(Protocol):
    """One local connection to one robot. The service holds exactly one per robot."""

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    def on_reported(self, callback: ReportedCallback) -> None: ...

    def on_connection(self, callback: ConnectionCallback) -> None: ...

    async def send_command(self, name: str, params: dict[str, Any] | None = None) -> None: ...

    async def publish_settings(self, values: dict[str, Any]) -> None:
        """Publish protocol keys as one `delta` message."""
        ...

    async def request_position(self, timeout: float) -> PositionFix | None:
        """A fix, `None` for "no fix", or raises `TimeoutError` when the robot doesn't answer."""
        ...


class RobotTransportFactory(Protocol):
    def __call__(self, robot_id: str, address: str, password: str) -> RobotTransport: ...


class PasswordRetriever(Protocol):
    async def retrieve(self, address: str, timeout: float) -> str:
        """The robot's local password, or raises `password_unavailable` / `timeout`."""
        ...


class CloudTransport(Protocol):
    """The unofficial iRobot cloud. Payloads are returned as plain JSON dicts."""

    async def login(self) -> None: ...

    async def robot_password(self, blid: str) -> str | None: ...

    async def pmaps(self, blid: str) -> list[dict[str, Any]]: ...

    async def umf(self, blid: str, pmap_id: str, version: str) -> dict[str, Any]: ...

    async def mission_history(self, blid: str) -> list[dict[str, Any]]: ...

    def watch_timeline(self, blid: str) -> AsyncIterator[dict[str, Any]]:
        """Live mission timeline reports for one robot, until cancelled."""
        ...

    async def close(self) -> None: ...


class CloudTransportFactory(Protocol):
    def __call__(self, username: str, password: str, country: str) -> CloudTransport: ...
