"""IRobotCloud against stand-ins for roombapy-prime's CloudAccount and PrimeFactory."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from roombapy_prime import AuthCredentialsError, CloudError, CloudErrorReason

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.transport import cloud
from roomba_kit.transport.base import CloudTransport, CloudTransportFactory
from roomba_kit.transport.cloud import IRobotCloud, parse_umf_layers

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5_ID = "00000000000000000000000000000001"
PREFIX = "v007-irbthbu"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class StubRest:
    error: BaseException | None = None

    async def _answer(self, value):
        if StubRest.error is not None:
            raise StubRest.error
        return value

    async def get_pmaps(self, blid):
        return await self._answer(fixture("i5_pmaps.json"))

    async def get_pmap_umf(self, blid, pmap_id, version):
        return await self._answer({"maps": [], "asked": [pmap_id, version]})

    async def get_mission_history(self, blid):
        return await self._answer(fixture("i5_mission_history.json"))


class StubAccount:
    logins: list[dict] = []
    error: BaseException | None = None

    def __init__(self):
        self.robots = {I5_ID: SimpleNamespace(password="robot-pw")}
        self.login_result = SimpleNamespace(irbt_topic_prefix_for=lambda blid: PREFIX)

    @classmethod
    async def login(cls, session, username, password, country, *, app_id):
        cls.logins.append({"country": country, "app_id": app_id})
        if cls.error is not None:
            raise cls.error
        return cls()

    def classic_rest(self):
        return StubRest()

    async def relogin(self):
        return self.login_result


class StubRobot:
    """Only the subscribe-side surface: any publish attempt would be an AttributeError."""

    def __init__(self, reports):
        self.reports = reports
        self.topics = []
        self.connected = False
        self.closed = False

    async def connect(self):
        self.connected = True

    async def watch_raw_topic(self, topic):
        self.topics.append(topic)
        for payload in self.reports:
            yield SimpleNamespace(topic=topic, payload=payload)
        await asyncio.Event().wait()

    async def disconnect(self):
        self.closed = True


@pytest.fixture
def stubs(monkeypatch):
    StubAccount.logins, StubAccount.error, StubRest.error = [], None, None
    timeline = [
        json.loads(line) for line in (FIXTURES / "i5_timeline.jsonl").read_text().splitlines()
    ]
    robots = []

    async def create_prime_robot(session, username, password, country, blid=None, **kwargs):
        assert kwargs["login_result"] is not None and kwargs["relogin"] is not None
        robots.append(StubRobot([timeline[0], json.dumps(timeline[1]), "not json"]))
        return robots[-1]

    monkeypatch.setattr(cloud, "CloudAccount", StubAccount)
    monkeypatch.setattr(
        cloud, "PrimeFactory", SimpleNamespace(create_prime_robot=create_prime_robot)
    )
    return SimpleNamespace(robots=robots, timeline=timeline)


@pytest.fixture
async def account(stubs):
    client = IRobotCloud("user@example.com", "secret", "US")
    yield client
    await client.close()


def test_class_is_a_cloud_factory():
    factory: CloudTransportFactory = IRobotCloud
    transport: CloudTransport = factory("u", "p", "US")
    assert "p" not in repr(transport).replace("country", "")


async def test_rest_calls_login_once(account):
    assert (await account.pmaps(I5_ID))[0]["pmap_id"] == "FbKwAujERSazAALZRD_rEg"
    assert len(await account.mission_history(I5_ID)) > 10
    assert (await account.umf(I5_ID, "map", "v1"))["asked"] == ["map", "v1"]
    assert await account.robot_password(I5_ID) == "robot-pw"
    assert await account.robot_password("unknown") is None
    assert len(StubAccount.logins) == 1
    assert StubAccount.logins[0]["app_id"].startswith("IOS-")


async def test_timeline_subscribes_and_never_publishes(account, stubs):
    stream = account.watch_timeline(I5_ID)
    first, second = await anext(stream), await anext(stream)
    assert first == stubs.timeline[0] and second == stubs.timeline[1]
    (robot,) = stubs.robots
    assert robot.topics == [f"{PREFIX}/things/{I5_ID}/mission/timeline/report"]
    assert robot.connected and not robot.closed
    await stream.aclose()
    assert robot.closed


@pytest.mark.parametrize(
    ("error", "code", "reason"),
    [
        (AuthCredentialsError("bad"), ErrorCode.AUTH_FAILED, None),
        (CloudError("x", reason=CloudErrorReason.ACCOUNT_LOCKED), ErrorCode.AUTH_FAILED, None),
        (
            CloudError("x", reason=CloudErrorReason.RATE_LIMITED),
            ErrorCode.CLOUD_UNAVAILABLE,
            "rate_limited",
        ),
        (aiohttp.ClientConnectionError("dns"), ErrorCode.CLOUD_UNAVAILABLE, "connection_failed"),
        (TimeoutError(), ErrorCode.CLOUD_UNAVAILABLE, "timeout"),
    ],
)
async def test_login_error_mapping(account, error, code, reason):
    StubAccount.error = error
    with pytest.raises(KitError) as err:
        await account.login()
    assert err.value.code is code and err.value.__cause__ is None
    assert (err.value.details or {}).get("reason") == reason
    assert "secret" not in err.value.message and "user@" not in err.value.message


async def test_rest_failure_maps_to_cloud_unavailable(account):
    StubRest.error = CloudError("boom", reason=CloudErrorReason.SERVER_ERROR)
    with pytest.raises(KitError) as err:
        await account.pmaps(I5_ID)
    assert err.value.code is ErrorCode.CLOUD_UNAVAILABLE
    assert err.value.details == {"reason": "server_error"}


def test_parse_umf_mission_fixture():
    (layers,) = parse_umf_layers(fixture("i5_mission_umf.json"))
    assert (layers.map_id, layers.map_version) == ("iQpmRZnFReKgtSH8YpG4UA", "260821T153709")
    assert len(layers.coverage) == 120 and len(layers.dirt) == 12
    assert len(layers.clutter) == 3 and len(layers.coverage_poly) == 1
    assert len(layers.coverage_poly[0]) == 24
    assert layers.point_area_m2 == pytest.approx(0.1049**2)
    assert all(isinstance(x, float) for x, _ in layers.coverage)


@pytest.mark.parametrize(
    ("name", "coverage", "clutter"),
    [("i5_mission_umf_small.json", 101, 0), ("m6_mission_umf.json", 252, 10)],
)
def test_parse_umf_captures(name, coverage, clutter):
    (layers,) = parse_umf_layers(fixture(f"captures/{name}"))
    assert len(layers.coverage) == coverage and len(layers.clutter) == clutter
    (outer, *_holes) = layers.coverage_poly[0]
    assert len(layers.coverage_poly) == 1 and outer[0] == outer[-1]


def test_parse_umf_tolerates_missing_layers():
    assert parse_umf_layers({}) == []
    (empty,) = parse_umf_layers({"maps": [{"layers": [{"layer_type": "deltas", "geometry": {}}]}]})
    assert empty.coverage == [] and empty.map_id is None
