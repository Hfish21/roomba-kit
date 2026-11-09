import os
from pathlib import Path

import pytest

from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import RobotEntry, load_settings
from roomba_kit.core.capabilities import Capability
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import ConnectionState, DiscoveredRobot
from roomba_kit.service import components
from roomba_kit.service.app import ServiceContext
from roomba_kit.transport.fake import FakeTransportFactory

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"


def _write_config(home: Path, robots: list[RobotEntry]) -> None:
    from roomba_kit.config.settings import ConfigModel, save_config

    save_config(ConfigModel(robots=robots))


@pytest.fixture
def factory():
    return FakeTransportFactory.from_fixtures(FIXTURES)


async def _until(predicate, timeout=2.0):
    import asyncio

    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def _ctx(factory, **kw):
    async def discover(broadcast, timeout_s):
        return [
            DiscoveredRobot(
                id=I5,
                name="Vacuum",
                sku="i5G5020",
                firmware="x",
                address="192.0.2.50",
                supported=True,
                profile_id="roomba_i5",
            ),
        ]

    return ServiceContext(
        settings=load_settings(),
        transport_factory=factory,
        discover=discover,
        session_options={"backoff_min": 0.01, "backoff_max": 0.02},
        **kw,
    )


async def test_starts_with_zero_robots(tmp_kit_home, factory):
    ctx = _ctx(factory)
    await ctx.start()
    try:
        assert ctx.sessions == {} and ctx.cloud.status().status == "not_configured"
        assert {"roomba_i5", "braava_m6"} <= set(ctx.profiles)
    finally:
        await ctx.stop()


async def test_sessions_from_config_and_resolution(tmp_kit_home, factory):
    _write_config(
        tmp_kit_home,
        [
            RobotEntry(id=I5, name="Vacuum", address="192.0.2.10", profile="roomba_i5"),
            RobotEntry(id=M6, name="Mop", address="192.0.2.11", profile="braava_m6"),
        ],
    )
    creds = CredentialStore()
    creds.set_robot_password(I5, "pw1")
    creds.set_robot_password(M6, "pw2")
    ctx = _ctx(factory)
    await ctx.start()
    try:
        await _until(lambda: all(s.connection is ConnectionState.CONNECTED for s in ctx))
        assert ctx.resolve_robot(I5).robot_id == I5
        assert ctx.resolve_robot("mop").robot_id == M6
        with pytest.raises(KitError) as exc:
            ctx.resolve_robot("Nobody")
        assert exc.value.code is ErrorCode.ROBOT_NOT_FOUND
        ctx.sessions[M6].name = "Vacuum"
        factory.get(M6).emit({"name": "Vacuum"})
        await _until(lambda: ctx.sessions[M6].robot().name == "Vacuum")
        with pytest.raises(KitError) as exc:
            ctx.resolve_robot("vacuum")
        assert exc.value.code is ErrorCode.AMBIGUOUS_NAME
        assert len(exc.value.details["candidates"]) == 2
        assert set(ctx.capability_support(Capability.EVAC)) == {"roomba_i5"}
    finally:
        await ctx.stop()


async def test_add_and_remove_robot_persist(tmp_kit_home, factory):
    ctx = _ctx(factory)
    await ctx.start()
    try:
        await ctx.add_robot(
            RobotEntry(id=I5, name="Vacuum", address="192.0.2.10", sku="i5G5020"), password="pw"
        )
        session = ctx.session(I5)
        assert session.profile is not None and session.profile.id == "roomba_i5"
        await _until(lambda: session.connection is ConnectionState.CONNECTED)
        assert load_settings().robot(I5) is not None
        assert CredentialStore().robot_password(I5) == "pw"
        assert await ctx.rediscover(I5) == "192.0.2.50"
        assert await ctx.remove_robot(I5)
        assert I5 not in ctx.sessions and load_settings().robot(I5) is None
        assert CredentialStore().robot_password(I5) is None
    finally:
        await ctx.stop()


async def test_refuses_to_start_with_broad_credentials(tmp_kit_home, factory):
    CredentialStore().set_robot_password(I5, "pw")
    path = CredentialStore().path
    os.chmod(path, 0o644)
    with pytest.raises(KitError) as exc:
        await _ctx(factory).start()
    assert "chmod 600" in exc.value.message


async def test_components_start_and_stop(tmp_kit_home, factory, monkeypatch):
    calls = []

    async def start(ctx):
        calls.append("start")

        async def stop():
            calls.append("stop")

        return stop

    monkeypatch.setattr(components, "_COMPONENTS", {"probe": start})
    ctx = _ctx(factory)
    await ctx.start()
    await ctx.stop()
    assert calls == ["start", "stop"]
