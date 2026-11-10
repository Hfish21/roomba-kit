"""The iRobot cloud adapter over roombapy-prime 0.8.3, and the only module importing it.

One `CloudAccount` login per account, shared by every robot, and one cloud MQTT session
per account carrying every robot's timeline subscription. This adapter only subscribes: it never
publishes, least of all to `.../req`, which AWS policy answers by dropping the session.
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import uuid4

import aiohttp
from roombapy_prime import (
    AuthCredentialsError,
    ClassicRestClient,
    CloudAccount,
    CloudError,
    CloudErrorReason,
    PrimeFactory,
)

from roomba_kit.core.errors import ErrorCode, KitError

log = logging.getLogger(__name__)

Point = tuple[float, float]
Polygon = list[list[Point]]
_AUTH_REASONS = {CloudErrorReason.CREDENTIALS_REJECTED, CloudErrorReason.ACCOUNT_LOCKED}


@dataclass
class UmfLayers:
    """One UMF map's coverage layers. A polygon is a list of rings, the outer one first."""

    map_id: str | None
    map_version: str | None
    coverage: list[Point] = field(default_factory=list)
    point_area_m2: float | None = None
    dirt: list[Point] = field(default_factory=list)
    clutter: list[Polygon] = field(default_factory=list)
    coverage_poly: list[Polygon] = field(default_factory=list)


def _points(raw: Any) -> list[Point]:
    return [(float(p[0]), float(p[1])) for p in raw or [] if isinstance(p, list) and len(p) >= 2]


def _polygons(raw: Any) -> list[Polygon]:
    return [[_points(ring) for ring in polygon or []] for polygon in raw or []]


def parse_umf_layers(umf: dict[str, Any]) -> list[UmfLayers]:
    """`maps[].layers[]` by `layer_type` (`coverage`, `dirt`, `clutter`, `coverage_poly`)."""
    result = []
    for umf_map in umf.get("maps") or []:
        header = umf_map.get("map_header") or {}
        parsed = UmfLayers(map_id=header.get("id"), map_version=header.get("version"))
        for layer in umf_map.get("layers") or []:
            geometry = layer.get("geometry") or {}
            coordinates = geometry.get("coordinates")
            kind = layer.get("layer_type")
            if kind == "coverage":
                parsed.coverage = _points(coordinates)
                area = geometry.get("point_area")
                if isinstance(area, list) and len(area) == 2:
                    parsed.point_area_m2 = float(area[0]) * float(area[1])
            elif kind == "dirt":
                parsed.dirt = _points(coordinates)
            elif kind == "clutter":
                parsed.clutter = _polygons(coordinates)
            elif kind == "coverage_poly":
                parsed.coverage_poly = _polygons(coordinates)
        result.append(parsed)
    return result


def _kit_error(err: BaseException) -> KitError:
    """Library failure → `auth_failed` or `cloud_unavailable`. Messages never carry its text."""
    reason = getattr(err, "reason", None)
    if isinstance(err, AuthCredentialsError) or reason in _AUTH_REASONS:
        return KitError(ErrorCode.AUTH_FAILED, "The iRobot cloud rejected the account login.")
    if isinstance(reason, CloudErrorReason):
        name = reason.value
    elif isinstance(err, TimeoutError):
        name = "timeout"
    else:
        name = "connection_failed"
    return KitError(
        ErrorCode.CLOUD_UNAVAILABLE, f"iRobot cloud unavailable: {name}", details={"reason": name}
    )


@contextlib.contextmanager
def _mapped() -> Iterator[None]:
    try:
        yield
    except KitError:
        raise
    except (CloudError, aiohttp.ClientError, TimeoutError, OSError, ValueError, KeyError) as err:
        raise _kit_error(err) from None


class _SharedTimeline:
    """One cloud MQTT session for the whole account, one timeline subscription per robot.

    Two PrimeRobot sessions under one login evict each other (R12 spike: 105 drops in 90 s), so
    the session is created once, from the first robot watched, and every later robot's topic is
    added to it. Never open a second one.
    """

    def __init__(self, cloud: "IRobotCloud") -> None:
        self._cloud = cloud
        self._robot: Any = None
        self._lock = asyncio.Lock()

    async def _session(self, blid: str) -> Any:
        async with self._lock:
            if self._robot is None:
                account, session = await self._cloud.account()
                with _mapped():
                    robot = await PrimeFactory.create_prime_robot(
                        session,
                        self._cloud.username,
                        self._cloud.password,
                        self._cloud.country,
                        blid=blid,
                        login_result=account.login_result,
                        relogin=account.relogin,
                    )
                    try:
                        await robot.connect()
                    except BaseException:
                        with contextlib.suppress(Exception):
                            await robot.disconnect()
                        raise
                self._robot = robot
            return self._robot

    async def stream(self, blid: str) -> AsyncGenerator[dict[str, Any]]:
        account, _ = await self._cloud.account()
        prefix = account.login_result.irbt_topic_prefix_for(blid)
        if prefix is None:
            raise KitError(
                ErrorCode.CLOUD_UNAVAILABLE,
                "iRobot cloud gave no topic prefix for this robot.",
                details={"reason": "response_malformed"},
            )
        robot = await self._session(blid)
        topic = f"{prefix}/things/{blid}/mission/timeline/report"
        with _mapped():
            raw = cast(AsyncGenerator[Any], robot.watch_raw_topic(topic))
            async with contextlib.aclosing(raw) as reports:
                async for report in reports:
                    payload = report.payload
                    if isinstance(payload, str):
                        with contextlib.suppress(ValueError):
                            payload = json.loads(payload)
                    if isinstance(payload, dict):
                        yield payload

    async def close(self) -> None:
        async with self._lock:
            robot, self._robot = self._robot, None
        if robot is not None:
            with contextlib.suppress(Exception):
                await robot.disconnect()


class IRobotCloud:
    """A `CloudTransport` for one account; the class itself is a `CloudTransportFactory`."""

    def __init__(self, username: str, password: str, country: str) -> None:
        self.username = username
        self.password = password
        self.country = country
        self._app_id = f"IOS-{uuid4()}"
        self._session: aiohttp.ClientSession | None = None
        self._account: CloudAccount | None = None
        self._rest: ClassicRestClient | None = None
        self._timeline = _SharedTimeline(self)

    def __repr__(self) -> str:
        return f"IRobotCloud(country={self.country!r}, logged_in={self._account is not None})"

    async def login(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        with _mapped():
            self._account = await CloudAccount.login(
                self._session, self.username, self.password, self.country, app_id=self._app_id
            )
        self._rest = self._account.classic_rest()

    async def account(self) -> tuple[CloudAccount, aiohttp.ClientSession]:
        if self._account is None or self._session is None:
            await self.login()
        assert self._account is not None and self._session is not None  # noqa: S101
        return self._account, self._session

    async def _classic(self) -> ClassicRestClient:
        if self._rest is None:
            await self.login()
        assert self._rest is not None  # noqa: S101
        return self._rest

    async def robot_password(self, blid: str) -> str | None:
        account, _ = await self.account()
        entry = account.robots.get(blid)
        return entry.password if entry is not None else None

    async def pmaps(self, blid: str) -> list[dict[str, Any]]:
        rest = await self._classic()
        with _mapped():
            return list(await rest.get_pmaps(blid))

    async def umf(self, blid: str, pmap_id: str, version: str) -> dict[str, Any]:
        rest = await self._classic()
        with _mapped():
            return dict(await rest.get_pmap_umf(blid, pmap_id, version))

    async def mission_history(self, blid: str) -> list[dict[str, Any]]:
        rest = await self._classic()
        with _mapped():
            records = await rest.get_mission_history(blid)
        return [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []

    def watch_timeline(self, blid: str) -> AsyncIterator[dict[str, Any]]:
        return self._timeline.stream(blid)

    async def close(self) -> None:
        await self._timeline.close()
        session, self._session = self._session, None
        self._account = self._rest = None
        if session is not None:
            await session.close()
