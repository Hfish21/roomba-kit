"""FR-003: the at-the-robot pairing steps come from the robot's model profile."""

import dataclasses

import httpx
import pytest
from click.testing import CliRunner

from roomba_kit.cli import main
from roomba_kit.cli.client import CliState
from roomba_kit.config.settings import RobotEntry, load_settings
from roomba_kit.core import registry
from roomba_kit.profiles import base
from roomba_kit.profiles.loader import load_profiles
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.operations import load_operations, setup
from roomba_kit.transport.fake import FakePasswordRetriever, FakeTransportFactory
from tests.unit.test_us1_cloud_account import FIXTURES, I5, M6
from tests.unit.test_us1_setup_wizard import MOP, Service, run

MOP_STEPS = ["Put the mop on its dock", "Hold HOME and SPOT for 5 seconds", "Release"]
DEFAULT = list(base.DEFAULT_PAIRING_STEPS)


def test_profiles_default_to_the_shared_steps():
    profile = base.ModelProfile(id="x", display_name="X", sku_patterns=[], capabilities={})
    assert profile.pairing_steps == DEFAULT
    assert setup.pairing_steps_for(None) == DEFAULT
    assert setup.pairing_steps_for("no_such_profile") == DEFAULT


@pytest.fixture
async def ctx(tmp_kit_home, monkeypatch):
    monkeypatch.setattr(setup, "RETRY_S", 0.01)
    load_operations()
    c = ServiceContext(
        settings=load_settings(),
        transport_factory=FakeTransportFactory.from_fixtures(FIXTURES),
        password_retriever=FakePasswordRetriever(cloud_only=["192.0.2.10", "192.0.2.11"]),
        session_options={"backoff_min": 0.01, "backoff_max": 0.02},
    )
    await c.start()
    await c.add_robot(RobotEntry(id=I5, name="Vacuum", address="192.0.2.10", sku="i5G5020"))
    await c.add_robot(RobotEntry(id=M6, name="Mop", address="192.0.2.11", sku="m611020"))
    yield c
    await c.stop()


async def _retrieve(ctx, robot):
    op = registry.get("retrieve_password")
    return op.dump(await registry.invoke(op, ctx, {"robot": robot, "timeout_s": 0.05}))


async def test_retrieve_password_returns_the_robots_profile_steps(ctx):
    mop = ctx.resolve_robot(M6)
    mop.profile = dataclasses.replace(mop.profile, pairing_steps=MOP_STEPS)
    assert (await _retrieve(ctx, "Mop")) == {"outcome": "cloud_only", "steps": MOP_STEPS}
    assert (await _retrieve(ctx, "Vacuum"))["steps"] == DEFAULT


async def test_unsupported_robot_gets_the_default_steps(ctx):
    ctx.resolve_robot(M6).profile = None
    assert (await _retrieve(ctx, M6))["steps"] == DEFAULT


def test_wizard_prints_the_profile_steps(tmp_kit_home, monkeypatch):
    # The wizard probes the real port before anything else; never let it start a service.
    from roomba_kit.lifecycle import daemon

    monkeypatch.setattr(daemon, "health", lambda url, timeout=1.0: {"status": "ok"})
    m6 = dataclasses.replace(load_profiles()["braava_m6"], pairing_steps=MOP_STEPS)
    monkeypatch.setitem(load_profiles(), "braava_m6", m6)
    service = Service(
        list_robots=[[]],
        discover_robots=[[MOP]],
        add_robot={**MOP, "configured": True},
        retrieve_password={"outcome": "stored", "steps": MOP_STEPS},
        cloud_status={"status": "not_configured"},
    )
    result = run(service, ["y", "", "n"])
    assert result.exit_code == 0, result.output
    assert all(step in result.output for step in MOP_STEPS), result.output
    assert DEFAULT[1] not in result.output


def test_robots_password_prints_the_profile_steps(tmp_kit_home, monkeypatch):
    m6 = dataclasses.replace(load_profiles()["braava_m6"], pairing_steps=MOP_STEPS)
    monkeypatch.setitem(load_profiles(), "braava_m6", m6)
    service = Service(get_robot=MOP, retrieve_password={"outcome": "stored", "steps": MOP_STEPS})
    obj = CliState(transport=httpx.MockTransport(service))
    result = CliRunner().invoke(main.build_cli(), ["robots", "password", "Mop"], obj=obj)
    assert result.exit_code == 0, result.output
    assert "1. Put the mop on its dock" in result.output
    assert service.names() == ["get_robot", "retrieve_password"]
