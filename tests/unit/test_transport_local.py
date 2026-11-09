"""LocalTransport against a stand-in RoombaClient, plus real roombapy over a stand-in aiomqtt."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import aiomqtt
import pytest
from roombapy import RobotPosition, RoombaAuthError, RoombaConnectionError, RrtpUnsupportedError
from roombapy import roomba as roomba_module

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import DiscoveredRobot
from roomba_kit.transport import local
from roomba_kit.transport.base import RobotTransportFactory
from roomba_kit.transport.local import LocalPasswordRetriever, LocalTransport, to_position_fix

I5_ID = "00000000000000000000000000000001"
M6_ID = "00000000000000000000000000000002"
ADDR = "192.0.2.10"


class StubClient:
    """Records calls; `connect_error` and `position` script the outcomes."""

    instances: list["StubClient"] = []
    connect_error: Exception | None = None
    position: object = None

    def __init__(self, address, blid, password):
        self.args = (address, blid, password)
        self.on_message = []
        self.on_state = []
        self.auth_error = None
        self.calls = []
        StubClient.instances.append(self)

    def register_on_message_callback(self, cb):
        self.on_message.append(cb)

    def register_on_connection_state_callback(self, cb):
        self.on_state.append(cb)

    async def connect(self):
        if StubClient.connect_error is not None:
            raise StubClient.connect_error
        for cb in self.on_state:
            cb("connected", None)

    async def disconnect(self):
        self.calls.append(("disconnect",))

    async def send_command(self, name, params=None):
        self.calls.append(("send_command", name, params))

    async def set_preferences(self, values):
        self.calls.append(("set_preferences", values))

    async def get_position(self, *, timeout):
        if isinstance(StubClient.position, BaseException):
            raise StubClient.position
        return StubClient.position

    def deliver(self, message):
        for cb in self.on_message:
            cb(message)

    def drop(self):
        for cb in self.on_state:
            cb("disconnected", "lost")


def robot(robot_id=I5_ID):
    return DiscoveredRobot(id=robot_id, name="", sku="", firmware="", address=ADDR, supported=False)


@pytest.fixture
def probe_answer(monkeypatch):
    answer = SimpleNamespace(value=None)

    async def fake_probe(address, timeout_s=3.0, **_):
        return answer.value

    monkeypatch.setattr(local.discovery, "probe", fake_probe)
    return answer


@pytest.fixture
def stub(monkeypatch, probe_answer):
    StubClient.instances, StubClient.connect_error, StubClient.position = [], None, None
    monkeypatch.setattr(local, "RoombaClient", StubClient)
    return StubClient


@pytest.fixture
async def link(stub):
    transport = LocalTransport(I5_ID, ADDR, "pw")
    links, reported = [], []
    transport.on_connection(lambda up, reason: links.append((up, reason)))
    transport.on_reported(reported.append)
    await transport.connect()
    yield transport, stub.instances[-1], links, reported
    await transport.disconnect()


def test_class_is_a_transport_factory():
    factory: RobotTransportFactory = LocalTransport
    assert "pw" not in repr(factory(I5_ID, ADDR, "pw"))


async def test_connect_feeds_reported_fragments(link):
    transport, client, links, reported = link
    assert client.args == (ADDR, I5_ID, "pw") and links == [(True, None)]
    client.deliver({"state": {"reported": {"batPct": 90}}})
    client.deliver({"reportType": "current"})
    assert reported == [{"batPct": 90}]


@pytest.mark.parametrize(
    ("error", "answer", "code"),
    [
        (RoombaAuthError("bad"), None, ErrorCode.AUTH_FAILED),
        (RoombaConnectionError("refused"), robot(), ErrorCode.CONNECTION_HELD_ELSEWHERE),
        (RoombaConnectionError("refused"), robot(M6_ID), ErrorCode.ROBOT_OFFLINE),
        (RoombaConnectionError("refused"), None, ErrorCode.ROBOT_OFFLINE),
        (TimeoutError(), None, ErrorCode.ROBOT_OFFLINE),
    ],
)
async def test_connect_error_mapping(stub, probe_answer, error, answer, code):
    stub.connect_error, probe_answer.value = error, answer
    with pytest.raises(KitError) as err:
        await LocalTransport(I5_ID, ADDR, "pw").connect()
    assert err.value.code is code and err.value.__cause__ is None
    assert "pw" not in err.value.message


async def test_drop_tears_down_and_reports_once(link, probe_answer):
    transport, client, links, _ = link
    probe_answer.value = robot()
    client.drop()
    await asyncio.sleep(0.01)
    assert client.calls == [("disconnect",)]
    assert links == [(True, None), (False, "connection_held_elsewhere")]
    with pytest.raises(KitError) as err:
        await transport.send_command("find")
    assert err.value.code is ErrorCode.ROBOT_OFFLINE


async def test_commands_settings_and_errors(link):
    transport, client, _, _ = link
    await transport.send_command("start", {"ordered": 1})
    await transport.publish_settings({"padWetness": {"disposable": 2, "reusable": 2}})
    assert client.calls == [
        ("send_command", "start", {"ordered": 1}),
        ("set_preferences", {"padWetness": {"disposable": 2, "reusable": 2}}),
    ]


async def test_position_mapping(link, stub):
    transport, _, _, _ = link
    assert await transport.request_position(1.0) is None
    stub.position = RobotPosition(0.85, 0.0, -0.12, 1794163126, "request", "map", "v1")
    fix = await transport.request_position(1.0)
    assert (fix.robot_id, fix.x_m, fix.y_m, fix.heading_rad, fix.map_id, fix.map_version) == (
        I5_ID,
        0.85,
        0.0,
        -0.12,
        "map",
        "v1",
    )
    for silent in (TimeoutError(), RrtpUnsupportedError("no")):
        stub.position = silent
        with pytest.raises(TimeoutError):
            await transport.request_position(0.1)
    stub.position = RoombaConnectionError("gone")
    with pytest.raises(KitError):
        await transport.request_position(0.1)


def test_m6_capture_units_pass_through():
    rows = json.loads(
        (Path(__file__).parents[1] / "fixtures/captures/m6_positions.json").read_text()
    )
    _, x, y, theta = rows[0]
    fix = to_position_fix(M6_ID, RobotPosition(x, y, theta, 0, "request"))
    assert (fix.x_m, fix.y_m, fix.heading_rad) == (x, y, theta)
    assert fix.at.tzinfo is UTC and fix.at <= datetime.now(UTC)


async def test_password_retriever_maps_none(monkeypatch):
    answers = iter(["s3cret", None])

    class StubPassword:
        def __init__(self, ip):
            pass

        async def get_password(self, timeout):
            return next(answers)

    monkeypatch.setattr(local, "RoombaPassword", StubPassword)
    assert await LocalPasswordRetriever().retrieve(ADDR, 1.0) == "s3cret"
    with pytest.raises(KitError) as err:
        await LocalPasswordRetriever().retrieve(ADDR, 1.0)
    assert err.value.code is ErrorCode.PASSWORD_UNAVAILABLE


class FakeMqtt:
    """Stands in for aiomqtt.Client under the real RoombaClient."""

    made = 0
    auth_reject = False

    def __init__(self, **kwargs):
        FakeMqtt.made += 1
        FakeMqtt.last = self
        self.published = []

    async def __aenter__(self):
        if FakeMqtt.auth_reject:
            raise aiomqtt.MqttError("Not authorized")
        return self

    async def __aexit__(self, *exc):
        return None

    async def subscribe(self, topic):
        return None

    async def publish(self, topic, payload):
        self.published.append((topic, json.loads(payload)))

    @property
    def messages(self):
        return self._messages()

    async def _messages(self):
        yield SimpleNamespace(topic="x", payload=b'{"state": {"reported": {"batPct": 50}}}')
        await asyncio.sleep(0.05)
        raise aiomqtt.MqttError("connection lost")


async def test_real_roombapy_is_single_shot(monkeypatch, probe_answer):
    """The library's own supervisor must not reconnect after a drop: we cancel it."""
    FakeMqtt.made, FakeMqtt.auth_reject = 0, False
    monkeypatch.setattr(roomba_module.aiomqtt, "Client", FakeMqtt)
    transport = LocalTransport(I5_ID, ADDR, "pw")
    links, reported = [], []
    transport.on_connection(lambda up, reason: links.append((up, reason)))
    transport.on_reported(reported.append)
    await transport.connect()
    settings = {"padWetness": {"disposable": 2, "reusable": 1}, "rankOverlap": 85}
    await transport.publish_settings(settings)
    assert FakeMqtt.last.published == [("delta", {"state": settings})]
    await asyncio.sleep(1.6)  # past roombapy's 1 s minimum reconnect backoff
    assert FakeMqtt.made == 1
    assert reported == [{"batPct": 50}]
    assert links == [(True, None), (False, "robot_offline")]


async def test_real_roombapy_auth_rejection(monkeypatch, probe_answer):
    FakeMqtt.made, FakeMqtt.auth_reject = 0, True
    monkeypatch.setattr(roomba_module.aiomqtt, "Client", FakeMqtt)
    with pytest.raises(KitError) as err:
        await LocalTransport(I5_ID, ADDR, "pw").connect()
    assert err.value.code is ErrorCode.AUTH_FAILED and FakeMqtt.made == 1
