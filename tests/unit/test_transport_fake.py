import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import PositionFix
from roomba_kit.transport.base import (
    CloudTransport,
    PasswordRetriever,
    RobotTransport,
    RobotTransportFactory,
)
from roomba_kit.transport.fake import (
    FIXTURE_I5_ID,
    FIXTURE_M6_ID,
    FakeCloud,
    FakePasswordRetriever,
    FakeTransport,
    FakeTransportFactory,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def phases(fragments):
    return [
        (f["cleanMissionStatus"]["cycle"], f["cleanMissionStatus"]["phase"])
        for f in fragments
        if "cleanMissionStatus" in f
    ]


@pytest.fixture
async def robot():
    factory = FakeTransportFactory.from_fixtures(FIXTURES)
    fake = factory(FIXTURE_I5_ID, "192.0.2.10", "pw")
    seen, links = [], []
    fake.on_reported(seen.append)
    fake.on_connection(lambda up, reason: links.append((up, reason)))
    await fake.connect()
    yield fake, seen, links
    await fake.disconnect()


def test_fakes_satisfy_the_protocols():
    transport: RobotTransport = FakeTransport("r")
    factory: RobotTransportFactory = FakeTransportFactory()
    cloud: CloudTransport = FakeCloud()
    retriever: PasswordRetriever = FakePasswordRetriever()
    assert transport and factory and cloud and retriever


async def test_connect_reports_link_and_full_state(robot):
    fake, seen, links = robot
    assert links == [(True, None)]
    assert seen[0]["sku"] == "i5G5020" and seen[0]["name"] == "Vacuum"


async def test_mission_command_reactions(robot):
    fake, seen, _ = robot
    seen.clear()
    for command in ("start", "pause", "resume", "dock"):
        await fake.send_command(command)
        await fake.settle()
    assert phases(seen) == [
        ("clean", "run"),
        ("clean", "stop"),
        ("clean", "run"),
        ("clean", "hmUsrDock"),
        ("none", "charge"),
    ]
    echoes = [f["lastCommand"] for f in seen if "lastCommand" in f]
    assert [e["command"] for e in echoes] == ["start", "pause", "resume", "dock"]
    assert all(e["initiator"] == "localApp" and e["time"] > 0 for e in echoes)
    assert fake.sent_commands[0] == ("start", None)


async def test_start_from_idle_opens_a_new_mission(robot):
    fake, _, _ = robot
    before = fake.reported["cleanMissionStatus"]["nMssn"]
    params = {"pmap_id": "p", "regions": [{"region_id": "7", "type": "rid"}]}
    await fake.send_command("start", params)
    await fake.settle()
    assert fake.reported["cleanMissionStatus"]["nMssn"] == before + 1
    assert fake.reported["lastCommand"]["regions"] == params["regions"]


async def test_evac_returns_to_previous_cycle(robot):
    fake, seen, _ = robot
    seen.clear()
    await fake.send_command("evac")
    await fake.settle()
    assert phases(seen) == [("evac", "charge"), ("none", "charge")]


async def test_reaction_delay_and_dropped_commands(robot):
    fake, seen, _ = robot
    fake.reaction_delay = 0.05
    fake.dropped_commands.add("pause")
    seen.clear()
    await fake.send_command("start")
    await asyncio.sleep(0)
    assert seen == []
    await fake.settle()
    assert phases(seen) == [("clean", "run")]
    await fake.send_command("pause")
    await fake.settle()
    assert phases(seen) == [("clean", "run")]
    assert [c for c, _ in fake.sent_commands] == ["start", "pause"]


async def test_settings_are_reflected_unless_ignored(robot):
    fake, seen, _ = robot
    fake.ignored_settings.add("carpetBoost")
    seen.clear()
    await fake.publish_settings({"twoPass": True, "noAutoPasses": False, "carpetBoost": True})
    await fake.settle()
    assert fake.published_settings == [
        {"twoPass": True, "noAutoPasses": False, "carpetBoost": True}
    ]
    assert seen == [{"twoPass": True, "noAutoPasses": False}]


async def test_scripted_connect_failures_then_success():
    fake = FakeTransport("r")
    fake.fail_next_connect(ErrorCode.AUTH_FAILED, ErrorCode.CONNECTION_HELD_ELSEWHERE)
    for code in (ErrorCode.AUTH_FAILED, ErrorCode.CONNECTION_HELD_ELSEWHERE):
        with pytest.raises(KitError) as err:
            await fake.connect()
        assert err.value.code is code
    await fake.connect()
    assert fake.connected and fake.connect_calls == 3


async def test_connect_checks_the_robots_password():
    factory = FakeTransportFactory()
    assert factory.get("r").robot_password is None
    await factory("r", "192.0.2.10", "anything").connect()  # unconfigured: any password works
    await factory.get("r").disconnect()

    factory.get("r").robot_password = "right"
    wrong = factory("r", "192.0.2.10", "wrong")
    with pytest.raises(KitError) as err:
        await wrong.connect()
    assert err.value.code is ErrorCode.AUTH_FAILED and not wrong.connected
    right = factory("r", "192.0.2.10", "right")
    await right.connect()
    assert right.connected


async def test_drop_and_restore(robot):
    fake, _, links = robot
    fake.drop("robot_offline")
    assert links[-1] == (False, "robot_offline")
    with pytest.raises(KitError) as err:
        await fake.send_command("find")
    assert err.value.code is ErrorCode.ROBOT_OFFLINE
    with pytest.raises(KitError):
        await fake.connect()
    fake.restore()
    await fake.connect()
    assert links[-1] == (True, None)


async def test_position_script_no_fix_and_silence(robot):
    fake, _, _ = robot
    fix = PositionFix(
        robot_id=FIXTURE_M6_ID, at=datetime.now(UTC), x_m=0.85, y_m=0.0, heading_rad=-0.12
    )
    fake.script_positions(fix, None)
    assert await fake.request_position(1.0) == fix
    assert await fake.request_position(1.0) is None
    assert await fake.request_position(1.0) is None
    fake.positions_never_answer = True
    with pytest.raises(TimeoutError):
        await fake.request_position(0.01)
    assert fake.position_requests == 4


def test_factory_keeps_one_fake_per_robot():
    factory = FakeTransportFactory.from_fixtures(FIXTURES)
    early = factory.get(FIXTURE_M6_ID)
    made = factory(FIXTURE_M6_ID, "192.0.2.11", "pw")
    assert made is early and made.address == "192.0.2.11"
    assert made.reported["padWetness"] == {"disposable": 3, "reusable": 3}


async def test_fake_cloud_serves_fixtures_and_timeline():
    cloud = FakeCloud.from_fixtures(FIXTURES)
    await cloud.login()
    maps = await cloud.pmaps(FIXTURE_I5_ID)
    assert maps[0]["pmap_id"] == "FbKwAujERSazAALZRD_rEg"
    assert len(await cloud.mission_history(FIXTURE_I5_ID)) > 10
    umf_key = next(iter(cloud.umf_by_key))
    assert (await cloud.umf(*umf_key))["maps"]
    timeline = aiter(cloud.watch_timeline(FIXTURE_I5_ID))
    first = await anext(timeline)
    assert first["event"][0]["type"] == "start"
    cloud.push_timeline(FIXTURE_M6_ID, {"event": [{"type": "fin"}]})
    assert await anext(aiter(cloud.watch_timeline(FIXTURE_M6_ID))) == {"event": [{"type": "fin"}]}


async def test_fake_cloud_fail_everything():
    cloud = FakeCloud.from_fixtures(FIXTURES)
    cloud.fail_everything = True
    for call in (cloud.login(), cloud.pmaps(FIXTURE_I5_ID), cloud.robot_password(FIXTURE_I5_ID)):
        with pytest.raises(KitError) as err:
            await call
        assert err.value.code is ErrorCode.CLOUD_UNAVAILABLE
        assert err.value.details == {"reason": "connection_failed"}
    cloud.fail_everything = False
    cloud.login_error = ErrorCode.AUTH_FAILED
    with pytest.raises(KitError) as err:
        await cloud.login()
    assert err.value.code is ErrorCode.AUTH_FAILED


async def test_fake_password_retriever():
    retriever = FakePasswordRetriever({"192.0.2.10": "pw"}, cloud_only={"192.0.2.12"})
    assert await retriever.retrieve("192.0.2.10", 1.0) == "pw"
    with pytest.raises(KitError) as err:  # silent, like the real adapter: HOME not held
        await retriever.retrieve("192.0.2.11", 1.0)
    assert err.value.code is ErrorCode.TIMEOUT
    with pytest.raises(KitError) as err:  # answered with the cloud-only reply
        await retriever.retrieve("192.0.2.12", 1.0)
    assert err.value.code is ErrorCode.PASSWORD_UNAVAILABLE
    slow = FakePasswordRetriever({"192.0.2.10": "pw"}, delay=1.0)
    with pytest.raises(KitError) as err:
        await slow.retrieve("192.0.2.10", 0.01)
    assert err.value.code is ErrorCode.TIMEOUT


def test_fixtures_hold_only_placeholder_ids():
    for path in FIXTURES.glob("*.json*"):
        text = path.read_text()
        assert "passwordHash" not in text and '"password"' not in text
    state = json.loads((FIXTURES / "i5_state.json").read_text())
    assert "state" not in state and state["sku"] == "i5G5020"
