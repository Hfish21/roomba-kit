"""US1 cloud account operations (T042)."""

from pathlib import Path

import pytest

from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import load_settings
from roomba_kit.core import registry
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.service import cloud
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.operations import load_operations
from roomba_kit.transport.fake import FakeTransportFactory
from tests.unit.test_cloud_manager import GOOD, Account

FIXTURES = Path(__file__).parents[1] / "fixtures"


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
