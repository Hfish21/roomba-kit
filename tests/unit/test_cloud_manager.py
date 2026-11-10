"""CloudManager: status, backoff, rejected credentials, events, configure/clear."""

import asyncio
from pathlib import Path

import pytest

from roomba_kit.config import settings as config
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import load_settings
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import EventType
from roomba_kit.service import cloud
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.cloud import CloudManager
from roomba_kit.transport.fake import FakeCloud, FakeTransportFactory

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"
GOOD = "right-horse-battery"


class Account:
    """A cloud factory: logins succeed only with password GOOD; `down` makes every login fail."""

    def __init__(self) -> None:
        self.down = False
        self.hang = False
        self.logins: list[tuple[str, str]] = []
        self.made: list[FakeCloud] = []

    def __call__(self, username: str, password: str, country: str) -> FakeCloud:
        account = self

        class Cloud(FakeCloud):
            async def login(self) -> None:
                account.logins.append((username, country))
                if account.hang:
                    await asyncio.Event().wait()
                if account.down:
                    raise KitError(
                        ErrorCode.CLOUD_UNAVAILABLE, "down", details={"reason": "connection_failed"}
                    )
                if password != GOOD:
                    raise KitError(ErrorCode.AUTH_FAILED, "rejected")
                await super().login()

        fake = Cloud(passwords={I5: "robot-pw"})
        self.made.append(fake)
        return fake


async def until(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
def account():
    return Account()


@pytest.fixture
async def ctx(tmp_kit_home, account, monkeypatch):
    monkeypatch.setattr(cloud, "BACKOFF_MIN_S", 0.01)
    monkeypatch.setattr(cloud, "BACKOFF_MAX_S", 0.04)
    monkeypatch.setattr(cloud, "POLL_S", 0.02)

    def make():
        return ServiceContext(
            settings=load_settings(),
            transport_factory=FakeTransportFactory.from_fixtures(FIXTURES),
            cloud_factory=account,
        )

    contexts = []

    async def started():
        c = make()
        contexts.append(c)
        await c.start()
        return c

    yield started
    for c in contexts:
        await c.stop()


async def statuses(c: ServiceContext) -> list[tuple[str, str | None]]:
    events, _ = await c.bus.read(types=[EventType.CLOUD_STATUS])
    return [(e.data["status"], e.data.get("reason")) for e in events]


async def until_status_event(c: ServiceContext, expected: tuple[str, str | None]) -> None:
    """The manager flips its status before awaiting the publish, so wait for the event."""
    async with asyncio.timeout(2.0):
        while not (got := await statuses(c)) or got[-1] != expected:
            await asyncio.sleep(0.005)


async def test_not_configured_without_credentials(ctx):
    c = await ctx()
    assert isinstance(c.cloud, CloudManager)
    assert c.cloud.status().status == "not_configured"
    with pytest.raises(KitError) as exc:
        c.cloud.transport()
    assert exc.value.code is ErrorCode.CLOUD_UNAVAILABLE
    assert exc.value.details == {"reason": "not_configured"}


async def test_disabled_stays_not_configured(ctx, account, monkeypatch):
    CredentialStore().set_cloud("me@example.com", GOOD)
    monkeypatch.setenv("ROOMBA_KIT_CLOUD__ENABLED", "false")
    c = await ctx()
    await asyncio.sleep(0.05)
    assert c.cloud.status().status == "not_configured" and c.cloud.status().reason == "disabled"
    assert account.logins == []
    with pytest.raises(KitError) as exc:
        await c.cloud.configure("me@example.com", GOOD, "US")
    assert exc.value.details == {"reason": "disabled"}


async def test_stored_credentials_log_in_in_the_background(ctx, account):
    CredentialStore().set_cloud("me@example.com", GOOD)
    c = await ctx()
    await until(lambda: c.cloud.status().status == "connected")
    status = c.cloud.status()
    assert status.last_ok is not None and status.reason is None
    assert c.cloud.transport() is account.made[-1]
    assert await c.cloud.robot_password(I5) == "robot-pw"
    assert account.logins == [("me@example.com", "US")]
    assert await statuses(c) == [("unavailable", "connecting"), ("connected", None)]


async def test_backoff_doubles_to_the_cap_then_recovers(ctx, account):
    CredentialStore().set_cloud("me@example.com", GOOD)
    account.down = True
    c = await ctx()
    await until(lambda: len(c.cloud.backoff_history) >= 5)
    assert c.cloud.backoff_history[:5] == [0.01, 0.02, 0.04, 0.04, 0.04]
    status = c.cloud.status()
    assert (status.status, status.reason) == ("unavailable", "connection_failed")
    with pytest.raises(KitError) as exc:
        c.cloud.transport()
    assert exc.value.details == {"reason": "connection_failed"}
    account.down = False
    await until(lambda: c.cloud.status().status == "connected")
    await until_status_event(c, ("connected", None))


async def test_rejected_credentials_are_not_retried_until_they_change(ctx, account):
    CredentialStore().set_cloud("me@example.com", "wrong")
    c = await ctx()
    await until(lambda: c.cloud.status().reason == "auth_failed")
    tries = len(account.logins)
    c.cloud.refresh()
    await asyncio.sleep(0.1)
    assert len(account.logins) == tries == 1
    CredentialStore().set_cloud("me@example.com", GOOD)  # edited out of band
    await until(lambda: c.cloud.status().status == "connected")


async def test_configure_validates_before_saving(ctx, account):
    c = await ctx()
    with pytest.raises(KitError) as exc:
        await c.cloud.configure("me@example.com", "wrong", "GB")
    assert exc.value.code is ErrorCode.AUTH_FAILED
    assert CredentialStore().cloud() is None
    assert config.load_config().cloud.country == "US"
    assert c.cloud.status().status == "not_configured"

    status = await c.cloud.configure("me@example.com", GOOD, "GB")
    assert status.status == "connected"
    assert CredentialStore().cloud().password == GOOD
    assert config.load_config().cloud.country == "GB"
    assert account.logins == [("me@example.com", "GB"), ("me@example.com", "GB")]
    await asyncio.sleep(0.1)
    assert len(account.logins) == 2  # the validated login is adopted, not repeated


async def test_failed_configure_keeps_the_working_login(ctx, account):
    CredentialStore().set_cloud("me@example.com", GOOD)
    c = await ctx()
    await until(lambda: c.cloud.status().status == "connected")
    working = c.cloud.transport()
    with pytest.raises(KitError):
        await c.cloud.configure("other@example.com", "wrong", "US")
    assert c.cloud.transport() is working
    assert CredentialStore().cloud().username == "me@example.com"


async def test_clear_logs_out(ctx, account):
    CredentialStore().set_cloud("me@example.com", GOOD)
    c = await ctx()
    await until(lambda: c.cloud.status().status == "connected")
    transport = c.cloud.transport()
    status = await c.cloud.clear()
    assert status.status == "not_configured"
    assert CredentialStore().cloud() is None and transport.closed
    assert (await statuses(c))[-1] == ("not_configured", None)


async def test_connection_failures_trigger_a_relogin_but_data_errors_dont(ctx, account):
    CredentialStore().set_cloud("me@example.com", GOOD)
    c = await ctx()
    await until(lambda: c.cloud.status().status == "connected")
    c.cloud.report_failure(
        KitError(ErrorCode.CLOUD_UNAVAILABLE, "x", details={"reason": "not_yet_published"})
    )
    await asyncio.sleep(0.01)
    assert len(account.logins) == 1
    c.cloud.report_failure(
        KitError(ErrorCode.CLOUD_UNAVAILABLE, "x", details={"reason": "connection_broken"})
    )
    await until(lambda: len(account.logins) == 2)
    await until(lambda: c.cloud.transport() is account.made[-1])
    assert account.made[0].closed


async def test_a_hanging_cloud_never_delays_startup_or_local_status(ctx, account):
    CredentialStore().set_cloud("me@example.com", GOOD)
    account.hang = True
    async with asyncio.timeout(1.0):
        c = await ctx()
    await until(lambda: account.logins)
    status = c.cloud.status()
    assert (status.status, status.reason) == ("unavailable", "connecting")
    with pytest.raises(KitError):
        c.cloud.transport()
