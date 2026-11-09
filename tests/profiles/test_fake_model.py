"""Adding a model needs no surface change (SC-011, quickstart V10).

A made-up profile is installed through a patched entry-point list. It must show up on all
three generated surfaces, and nothing under `api/`, `mcp/` or `cli/` may name a profile.
"""

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from fastmcp import Client

import roomba_kit
from roomba_kit.api.app import create_app
from roomba_kit.cli.main import build_cli
from roomba_kit.config.settings import RobotEntry, load_settings
from roomba_kit.core import registry
from roomba_kit.core.capabilities import Capability, Verification
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import ConnectionState
from roomba_kit.core.registry import Operation
from roomba_kit.mcp.server import build_mcp, in_process
from roomba_kit.profiles import loader
from roomba_kit.profiles.base import ModelProfile
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.eventbus import EventBus
from roomba_kit.service.operations import load_operations
from roomba_kit.service.session import RobotSession
from roomba_kit.service.store import Store
from roomba_kit.transport.discovery import parse_reply
from roomba_kit.transport.fake import FakeTransportFactory

FAKE = ModelProfile(
    id="fake_model",
    display_name="Fake Model",
    sku_patterns=[r"^zq9"],
    capabilities={
        Capability.DOCK: Verification.unverified(),
        Capability.FIND: Verification.unverified(),
        Capability.LIVE_POSITION: Verification.unverified(),
    },
)
UNKNOWN_ID = "0" * 31 + "3"
SURFACE_PACKAGES = ("api", "mcp", "cli")


class Ctx:
    version = "test"


def _matcher(sku: str) -> str | None:
    profile = loader.match_profile(sku)
    return profile.id if profile else None


@pytest.fixture
def fake_profile(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModelProfile]:
    """The shipped profiles plus `FAKE`, as if a third-party package had been installed."""
    real = list(loader.entry_points(group=loader.ENTRY_POINT_GROUP))
    fake = SimpleNamespace(name=FAKE.id, load=lambda: FAKE)
    monkeypatch.setattr(loader, "entry_points", lambda group: [*real, fake])
    loader.load_profiles.cache_clear()
    try:
        yield FAKE
    finally:
        loader.load_profiles.cache_clear()


@pytest.fixture
def capability_ops(fake_profile: ModelProfile) -> list[Operation]:
    """Every real registered op gated on a capability FAKE declares."""
    load_operations()
    ops = [op for op in registry.all() if op.capability in fake_profile.capabilities]
    assert ops, "no registered operation uses a capability FAKE declares"
    return ops


def _assert_lists_fake(description: str) -> None:
    lines = description.splitlines()
    supported = next(line for line in lines if line.startswith("Supported on: "))
    assert FAKE.id in supported.removeprefix("Supported on: ").split(", ")
    verification = next(line for line in lines if line.startswith("Verification: "))
    assert f"{FAKE.id}: unverified" in verification


async def test_appears_in_mcp_tool_descriptions(capability_ops):
    async with Client(build_mcp(in_process(Ctx()))) as client:
        tools = {t.name: t for t in await client.list_tools()}
    ops = [op for op in capability_ops if "mcp" in op.surfaces]
    assert ops
    for op in ops:
        _assert_lists_fake(tools[op.name].description)


def test_appears_in_openapi_descriptions(capability_ops, tmp_kit_home):
    ctx = ServiceContext(settings=load_settings(), transport_factory=FakeTransportFactory())
    doc = create_app(ctx).openapi()
    by_id = {op["operationId"]: op for path in doc["paths"].values() for op in path.values()}
    ops = [op for op in capability_ops if op.http is not None and "http" in op.surfaces]
    assert ops
    for op in ops:
        _assert_lists_fake(by_id[op.name]["description"])


def test_appears_in_cli_help(capability_ops):
    root = build_cli(capability_ops)
    ops = [op for op in capability_ops if op.cli is not None and "cli" in op.surfaces]
    assert ops
    for op in ops:
        assert op.cli is not None
        result = CliRunner().invoke(root, [*op.cli.group, op.cli.name, "--help"])
        assert result.exit_code == 0, result.output
        assert "Supported on:" in result.output and FAKE.id in result.output
        assert f"{FAKE.id}: unverified" in result.output


def test_surface_modules_never_name_a_profile():
    loader.load_profiles.cache_clear()
    ids = set(loader.load_profiles()) | {"roomba_i5", "braava_m6"}
    package = Path(roomba_kit.__file__).parent
    offenders = [
        f"{path.relative_to(package)}: {pid}"
        for pkg in SURFACE_PACKAGES
        for path in sorted((package / pkg).rglob("*.py"))
        for pid in ids
        if pid in path.read_text()
    ]
    assert offenders == []


def test_discovery_matches_the_new_profile(fake_profile):
    reply = json.dumps({"robotid": UNKNOWN_ID, "sku": "zq9100", "ip": "192.0.2.30"}).encode()
    robot = parse_reply(reply, "192.0.2.30", _matcher)
    assert robot is not None
    assert (robot.supported, robot.profile_id) == (True, FAKE.id)


def test_unknown_sku_is_unsupported_in_discovery(fake_profile):
    reply = json.dumps({"robotid": UNKNOWN_ID, "sku": "x7000", "ip": "192.0.2.31"}).encode()
    robot = parse_reply(reply, "192.0.2.31", _matcher)
    assert robot is not None
    assert (robot.supported, robot.profile_id) == (False, None)


async def test_commands_on_a_robot_without_a_profile_fail_before_contact(tmp_path):
    store = await Store(tmp_path / "state.db").open()
    bus = EventBus(store)
    await bus.start()
    factory = FakeTransportFactory({UNKNOWN_ID: {"sku": "x7000", "batPct": 80}})
    entry = RobotEntry(id=UNKNOWN_ID, name="Other", address="192.0.2.32", sku="x7000")
    session = RobotSession(
        entry, None, factory, lambda: "pw", bus, backoff_min=0.01, backoff_max=0.02
    )
    await session.start()
    try:
        async with asyncio.timeout(2):
            while session.connection is not ConnectionState.CONNECTED:
                await asyncio.sleep(0.005)
        assert session.robot().supported is False
        for command in ("dock", "find", "clean"):
            with pytest.raises(KitError) as info:
                await session.execute(command)
            assert info.value.code is ErrorCode.UNSUPPORTED_MODEL
        assert factory.get(UNKNOWN_ID).sent_commands == []
    finally:
        await session.stop()
        await bus.close()
        await store.close()
