"""US1 cloud account and robot-password operations (T041 `set_robot_password`, T042 ops)."""

from pathlib import Path

import pytest

from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import RobotEntry, load_settings
from roomba_kit.core import registry
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import ConnectionState
from roomba_kit.service import cloud
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.operations import load_operations
from roomba_kit.transport.fake import FakeTransportFactory
from tests.unit.test_cloud_manager import GOOD, Account, until

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"


@pytest.fixture
def factory():
    return FakeTransportFactory.from_fixtures(FIXTURES)


@pytest.fixture
async def ctx(tmp_kit_home, factory, monkeypatch):
    monkeypatch.setattr(cloud, "POLL_S", 0.02)
    load_operations()
    c = ServiceContext(
        settings=load_settings(),
        transport_factory=factory,
        cloud_factory=Account(),
        session_options={"backoff_min": 0.01, "backoff_max": 0.02},
    )
    await c.start()
    await c.add_robot(RobotEntry(id=I5, name="Vacuum", address="192.0.2.10", sku="i5G5020"))
    await c.add_robot(RobotEntry(id=M6, name="Mop", address="192.0.2.11", sku="m611020"))
    yield c
    await c.stop()


async def call(ctx, name, **params):
    return registry.get(name).dump(await registry.invoke(registry.get(name), ctx, params))


async def test_cloud_login_status_and_logout(ctx):
    assert (await call(ctx, "cloud_status"))["status"] == "not_configured"
    failed = await call(ctx, "cloud_login", username="me@example.com", password="nope")
    assert failed == {"status": "failed", "reason": "auth_failed"}
    assert CredentialStore().cloud() is None
    ok = await call(ctx, "cloud_login", username="me@example.com", password=GOOD, country="gb")
    assert ok == {"status": "connected", "reason": None}
    assert ctx.settings.cloud.country == "GB"
    status = await call(ctx, "cloud_status")
    assert status["status"] == "connected" and status["last_ok"]
    assert (await call(ctx, "cloud_logout"))["status"] == "not_configured"
    assert CredentialStore().cloud() is None


async def test_cloud_login_rejects_a_bad_country(ctx):
    with pytest.raises(KitError) as exc:
        await call(ctx, "cloud_login", username="me@example.com", password=GOOD, country="USA")
    assert exc.value.code is ErrorCode.INVALID_REQUEST


async def test_cloud_retrieve_password(ctx, factory):
    result = await call(ctx, "cloud_retrieve_password", robot="Vacuum")
    assert result == {"outcome": "unavailable", "reason": "not_configured"}
    await call(ctx, "cloud_login", username="me@example.com", password=GOOD)
    result = await call(ctx, "cloud_retrieve_password", robot="Vacuum")
    assert result == {"outcome": "stored", "reason": None}
    assert CredentialStore().robot_password(I5) == "robot-pw"
    assert ctx.session(I5).connection is ConnectionState.CONNECTED
    assert factory.get(I5).password == "robot-pw"
    missing = await call(ctx, "cloud_retrieve_password", robot="Mop")
    assert missing == {"outcome": "unavailable", "reason": "not_in_account"}


async def test_set_robot_password_is_verified_by_connecting(ctx, factory):
    assert await call(ctx, "set_robot_password", robot=I5, password="first") == {
        "outcome": "stored"
    }
    assert ctx.session(I5).connection is ConnectionState.CONNECTED
    assert CredentialStore().robot_password(I5) == "first"

    factory.get(I5).fail_next_connect(ErrorCode.AUTH_FAILED)
    rejected = await call(ctx, "set_robot_password", robot=I5, password="second")
    assert rejected == {"outcome": "auth_failed"}
    assert CredentialStore().robot_password(I5) == "first"
    await until(lambda: ctx.session(I5).connection is ConnectionState.CONNECTED)
    assert factory.get(I5).password == "first"


async def test_unverifiable_password_is_not_saved(ctx, factory):
    factory.get(M6).drop(offline=True)
    with pytest.raises(KitError) as exc:
        await call(ctx, "set_robot_password", robot=M6, password="pw")
    assert exc.value.code is ErrorCode.ROBOT_OFFLINE
    assert CredentialStore().robot_password(M6) is None
    await until(lambda: ctx.session(M6).connection is ConnectionState.NOT_CONFIGURED)


async def test_env_override_is_reported(ctx, monkeypatch):
    monkeypatch.setenv(f"ROOMBA_KIT_ROBOTS__{I5}__PASSWORD", "from-env")
    with pytest.raises(KitError) as exc:
        await call(ctx, "set_robot_password", robot=I5, password="pw")
    assert exc.value.code is ErrorCode.INVALID_REQUEST
    monkeypatch.delenv(f"ROOMBA_KIT_ROBOTS__{I5}__PASSWORD")
    assert CredentialStore().robot_password(I5) is None
