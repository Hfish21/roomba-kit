"""A typed async Python client for the kit's HTTP API (T067; see docs/guides/building-consumers.md).

    async with Client("http://127.0.0.1:8765", token) as kit:
        result = await kit.dock("Vacuum")
        async for event in kit.events(types=[EventType.MISSION_FINISHED]):
            ...

There is one method per HTTP operation, named after it, taking its params and returning its
result model. Optional params default to `None`, which leaves the service's default in place.
Failures raise `KitError` with the catalogue code (contracts/errors.md); a command the robot
didn't confirm is not an error, it returns a `CommandResult` with `outcome="unconfirmed"`.
"""

# Each method returns `_call(...)`, which validates the body against the operation's own result
# type at runtime; tests/unit/test_client.py checks every annotation equals that type.
# mypy: disable-error-code="no-any-return"

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

import httpx
from pydantic_core import to_jsonable_python

from roomba_kit.config.tokens import read_cli_token
from roomba_kit.core import registry
from roomba_kit.core.capabilities import CapabilityStatus
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import Event, EventType
from roomba_kit.core.models import (
    CommandResult,
    CoverageRecord,
    DiscoveredRobot,
    Mission,
    MissionDetail,
    MissionOutcome,
    NoFix,
    PositionFix,
    Robot,
    RobotState,
    Room,
    SettingChange,
)
from roomba_kit.core.registry import Operation
from roomba_kit.service.app import CloudStatus
from roomba_kit.service.cloud_data import CloudMission, FloorPlan
from roomba_kit.service.operations import load_operations
from roomba_kit.service.verify import VerificationRun

if TYPE_CHECKING:
    # Annotations only. Importing an operations module registers its operations, which must
    # happen through `load_operations()` (it skips a test's isolated registry).
    from roomba_kit.service.operations.cloud_account import (
        CloudLoginResult,
        CloudPasswordResult,
    )
    from roomba_kit.service.operations.events import EventPage, MissionWaitTimeout
    from roomba_kit.service.operations.rooms import ImportResult, RoomList, RoomOptions
    from roomba_kit.service.operations.setup import PasswordRetrieval, PasswordStored, Removed
    from roomba_kit.service.operations.system import ConfigView, Health, ServiceStatus

__all__ = ["DEFAULT_URL", "Client", "error_from"]

DEFAULT_URL = "http://127.0.0.1:8765"
NOT_RUNNING = "The Roomba Kit service isn't running. Start it with: roomba-kit service start"


def error_from(body: Any) -> KitError | None:
    """The `KitError` in an `{"error": {...}}` envelope, or None if `body` isn't one."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None
    try:
        code = ErrorCode(str(error.get("code")))
    except ValueError:
        code = ErrorCode.INTERNAL
    robot_id = error.get("robot_id")
    details = error.get("details")
    return KitError(
        code,
        str(error.get("message", "")),
        robot_id=robot_id if isinstance(robot_id, str) else None,
        details=details if isinstance(details, dict) else None,
    )


def _check(response: httpx.Response, body: Any) -> None:
    if (err := error_from(body)) is not None:
        raise err
    if response.is_error or body is None:
        message = f"Unexpected response from the service: HTTP {response.status_code}"
        raise KitError(ErrorCode.INTERNAL, message)


async def _sse(lines: AsyncIterator[str]) -> AsyncIterator[Event]:
    """Events from an SSE line stream. Pings (comments) and frames without data are skipped."""
    data: list[str] = []
    async for line in lines:
        if line == "":
            if data:
                yield Event.model_validate_json("\n".join(data))
            data = []
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))


class Client:
    """The kit's HTTP API as async methods. Use as `async with Client(...) as kit:`.

    `token` defaults to `ROOMBA_KIT_API_TOKEN`, else this machine's `cli` token. `transport`
    is an httpx test seam (e.g. `httpx.ASGITransport(app)`).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_timeout_s: float = 5.0,
    ) -> None:
        load_operations()
        token = token if token is not None else read_cli_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # No read timeout: wait operations hold the request open for their own `timeout_s`.
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/v1",
            headers=headers,
            transport=transport,
            timeout=httpx.Timeout(connect_timeout_s, read=None),
        )

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- generic ---------------------------------------------------------------------------------

    async def request(self, operation: str | Operation, params: Mapping[str, Any]) -> Any:
        """Run `operation` with raw `params`; return its wire JSON or raise its `KitError`.

        Params are validated locally first, with the same `invalid_request` the service gives.
        """
        op = registry.get(operation) if isinstance(operation, str) else operation
        if op.http is None:
            raise KitError(ErrorCode.INVALID_REQUEST, f"{op.name} isn't on the HTTP API")
        registry.parse_params(op, params)
        req = registry.http_request(op, params)
        try:
            response = await self._http.request(
                req.method, req.path, params=req.query, json=req.body
            )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise KitError(ErrorCode.SERVICE_UNAVAILABLE, NOT_RUNNING) from None
        except httpx.TransportError:
            message = "Lost the connection to the service."
            raise KitError(ErrorCode.SERVICE_UNAVAILABLE, message) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        _check(response, body)
        return body

    async def _call(self, operation: str, **params: Any) -> Any:
        # Models, enums and datetimes in params become their JSON form, as the service reads it.
        raw = to_jsonable_python({k: v for k, v in params.items() if v is not None})
        wire = await self.request(operation, raw)
        return registry.get(operation).result_adapter.validate_python(wire)

    async def events(
        self,
        since: int | None = None,
        *,
        robot: str | None = None,
        types: list[EventType] | None = None,
        retry_s: float = 1.0,
    ) -> AsyncIterator[Event]:
        """Follow the SSE event stream, reconnecting with `Last-Event-ID` after any drop.

        With `since`, starts after that seq (replaying anything missed); without it, starts
        live. After the first event, reconnects never skip or repeat an event. Stops only when
        the caller stops iterating, or with a `KitError` the service returns (e.g. an unknown
        robot). While the service is unreachable it retries every `retry_s`.
        """
        op = registry.get("events_list")
        query = registry.http_request(op, {"robot": robot, "types": types}).query
        last = since
        while True:
            headers = {"Accept": "text/event-stream"}
            if last is not None:
                headers["Last-Event-ID"] = str(last)
            try:
                async with self._http.stream(
                    "GET", "/events", params=query, headers=headers
                ) as response:
                    if response.status_code != 200 or "text/event-stream" not in (
                        response.headers.get("content-type", "")
                    ):
                        try:
                            body = json.loads(await response.aread())
                        except ValueError:
                            body = None
                        _check(response, body)
                        raise KitError(ErrorCode.INTERNAL, "The service didn't send a stream.")
                    async for event in _sse(response.aiter_lines()):
                        last = event.seq
                        yield event
            except httpx.TransportError:
                pass
            await asyncio.sleep(retry_s)

    # -- system & setup --------------------------------------------------------------------------

    async def health(self) -> Health:
        """Whether the service is up, and its version. Needs no token."""
        return await self._call("health")

    async def service_status(self) -> ServiceStatus:
        """The service's version and uptime, each robot's connection, and the cloud's status."""
        return await self._call("service_status")

    async def config_show(self) -> ConfigView:
        """The effective configuration, with every secret shown as `"***"`."""
        return await self._call("config_show")

    async def discover_robots(
        self, broadcast: str | None = None, timeout_s: float | None = None
    ) -> list[DiscoveredRobot]:
        """Find iRobot robots on the local network."""
        return await self._call("discover_robots", broadcast=broadcast, timeout_s=timeout_s)

    async def add_robot(self, address: str) -> DiscoveredRobot:
        """Add the robot at an IP address, for when discovery can't reach it."""
        return await self._call("add_robot", address=address)

    async def retrieve_password(
        self, robot: str, timeout_s: float | None = None
    ) -> PasswordRetrieval:
        """Retrieve the robot's local password while you hold HOME on the docked robot."""
        return await self._call("retrieve_password", robot=robot, timeout_s=timeout_s)

    async def set_robot_password(self, robot: str, password: str) -> PasswordStored:
        """Store a robot's local password you already know, after checking the robot takes it."""
        return await self._call("set_robot_password", robot=robot, password=password)

    async def remove_robot(self, robot: str) -> Removed:
        """Remove a robot: stop its connection and delete its config and password."""
        return await self._call("remove_robot", robot=robot)

    # -- cloud account ---------------------------------------------------------------------------

    async def cloud_login(
        self, username: str, password: str, country: str | None = None
    ) -> CloudLoginResult:
        """Add or change the iRobot account login."""
        return await self._call(
            "cloud_login", username=username, password=password, country=country
        )

    async def cloud_logout(self) -> CloudStatus:
        """Remove the stored iRobot account login and disconnect from the cloud."""
        return await self._call("cloud_logout")

    async def cloud_status(self) -> CloudStatus:
        """The cloud subsystem's status."""
        return await self._call("cloud_status")

    async def cloud_retrieve_password(self, robot: str) -> CloudPasswordResult:
        """Fetch a robot's local password from the iRobot account, check it, and store it."""
        return await self._call("cloud_retrieve_password", robot=robot)

    # -- inventory & state -----------------------------------------------------------------------

    async def list_robots(self) -> list[Robot]:
        """Every configured robot."""
        return await self._call("list_robots")

    async def get_robot(self, robot: str) -> Robot:
        """One robot's details."""
        return await self._call("get_robot", robot=robot)

    async def get_state(self, robot: str) -> RobotState:
        """A robot's full live state."""
        return await self._call("get_state", robot=robot)

    async def get_capabilities(self, robot: str) -> list[CapabilityStatus]:
        """What the robot's model supports, each `verified` or `unverified`."""
        return await self._call("get_capabilities", robot=robot)

    # -- commands --------------------------------------------------------------------------------

    async def clean(self, robot: str) -> CommandResult:
        """Start cleaning the whole home."""
        return await self._call("clean", robot=robot)

    async def pause(self, robot: str) -> CommandResult:
        """Pause the current mission."""
        return await self._call("pause", robot=robot)

    async def resume(self, robot: str) -> CommandResult:
        """Resume a paused mission."""
        return await self._call("resume", robot=robot)

    async def stop(self, robot: str) -> CommandResult:
        """End the current mission where the robot is."""
        return await self._call("stop", robot=robot)

    async def dock(self, robot: str) -> CommandResult:
        """Send the robot back to its dock."""
        return await self._call("dock", robot=robot)

    async def find(self, robot: str) -> CommandResult:
        """Play the locate sound."""
        return await self._call("find", robot=robot)

    async def evac(self, robot: str) -> CommandResult:
        """Empty the bin into the Clean Base."""
        return await self._call("evac", robot=robot)

    async def reboot(self, robot: str) -> CommandResult:
        """Soft-reboot the robot."""
        return await self._call("reboot", robot=robot)

    async def train(self, robot: str) -> CommandResult:
        """Start a mapping run with no cleaning."""
        return await self._call("train", robot=robot)

    # -- rooms -----------------------------------------------------------------------------------

    async def clean_rooms(
        self,
        robot: str,
        rooms: list[str],
        ordered: bool | None = None,
        room_options: dict[str, RoomOptions] | None = None,
    ) -> CommandResult:
        """Clean only the given rooms (names or IDs), in order unless `ordered=False`."""
        return await self._call(
            "clean_rooms", robot=robot, rooms=rooms, ordered=ordered, room_options=room_options
        )

    async def list_rooms(self, robot: str) -> RoomList:
        """The robot's rooms: imported from the cloud, named locally, or learned."""
        return await self._call("list_rooms", robot=robot)

    async def import_rooms(self, robot: str) -> ImportResult:
        """Import the robot's room IDs and names from its stored map in the iRobot cloud."""
        return await self._call("import_rooms", robot=robot)

    async def name_room(self, robot: str, room_id: str, name: str) -> Room:
        """Give a room on the robot's current map a local name."""
        return await self._call("name_room", robot=robot, room_id=room_id, name=name)

    # -- settings --------------------------------------------------------------------------------

    async def get_settings(self, robot: str) -> dict[str, bool | int | str | None]:
        """Every setting the robot's model supports; null when not yet reported."""
        return await self._call("get_settings", robot=robot)

    async def set_setting(self, robot: str, setting: str, value: bool | int | str) -> SettingChange:
        """Change one setting, confirmed by read-back."""
        return await self._call("set_setting", robot=robot, setting=setting, value=value)

    # -- position --------------------------------------------------------------------------------

    async def get_position(self, robot: str) -> PositionFix | NoFix:
        """Where the robot is now, or `{no_fix: true}`."""
        return await self._call("get_position", robot=robot)

    # -- events ----------------------------------------------------------------------------------

    async def events_list(
        self,
        since: int | None = None,
        robot: str | None = None,
        types: list[EventType] | None = None,
        limit: int | None = None,
    ) -> EventPage:
        """Recorded events after `since`, oldest first. For a live stream use `events()`."""
        return await self._call("events_list", since=since, robot=robot, types=types, limit=limit)

    async def events_wait(
        self,
        since: int,
        robot: str | None = None,
        types: list[EventType] | None = None,
        timeout_s: float | None = None,
    ) -> EventPage:
        """Wait for events after `since`; empty at the timeout."""
        return await self._call(
            "events_wait", since=since, robot=robot, types=types, timeout_s=timeout_s
        )

    async def mission_wait(
        self, robot: str, mission_id: str | None = None, timeout_s: float | None = None
    ) -> Mission | MissionWaitTimeout:
        """Wait until a robot's mission ends and return it with its outcome."""
        return await self._call(
            "mission_wait", robot=robot, mission_id=mission_id, timeout_s=timeout_s
        )

    # -- history ---------------------------------------------------------------------------------

    async def list_missions(
        self,
        robot: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        outcome: MissionOutcome | None = None,
        limit: int | None = None,
    ) -> list[Mission]:
        """Missions recorded in local history, newest first."""
        return await self._call(
            "list_missions", robot=robot, since=since, until=until, outcome=outcome, limit=limit
        )

    async def get_mission(self, mission_id: str) -> MissionDetail:
        """One mission from local history, by its ID, with its activity transitions."""
        return await self._call("get_mission", mission_id=mission_id)

    # -- cloud records ---------------------------------------------------------------------------

    async def cloud_history(self, robot: str, limit: int | None = None) -> list[CloudMission]:
        """The robot's recent missions as the iRobot cloud recorded them, newest first."""
        return await self._call("cloud_history", robot=robot, limit=limit)

    async def get_coverage(self, robot: str, mission_id: str) -> CoverageRecord:
        """Where a finished mission cleaned."""
        return await self._call("get_coverage", robot=robot, mission_id=mission_id)

    async def get_floor_plan(self, robot: str) -> FloorPlan:
        """The robot's stored map from the iRobot cloud, with room outlines."""
        return await self._call("get_floor_plan", robot=robot)

    # -- verification ----------------------------------------------------------------------------

    async def verify(
        self,
        robot: str,
        capabilities: list[str] | None = None,
        allow_motion: bool | None = None,
    ) -> VerificationRun:
        """Check capabilities against the real robot and record the run."""
        return await self._call(
            "verify", robot=robot, capabilities=capabilities, allow_motion=allow_motion
        )
