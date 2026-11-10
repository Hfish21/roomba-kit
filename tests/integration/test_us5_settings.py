"""US5: settings are confirmed by read-back, related keys go together, unsupported ones
are refused before any robot contact. Runs against the fixture robots over the HTTP API."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from roomba_kit.api.app import create_app
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import ConfigModel, RobotEntry, load_settings, save_config
from roomba_kit.core import registry
from roomba_kit.core.models import ConnectionState
from roomba_kit.service import settings
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.operations import load_operations
from roomba_kit.transport.fake import FakeTransportFactory
from tests.unit.test_api import running

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"


def build_kit(factory: FakeTransportFactory) -> ServiceContext:
    """A service (not started) with the i5 ("Vacuum") and m6 ("Mop") fixtures configured."""
    save_config(
        ConfigModel(
            robots=[
                RobotEntry(id=I5, name="Vacuum", address="192.0.2.10", profile="roomba_i5"),
                RobotEntry(id=M6, name="Mop", address="192.0.2.11", profile="braava_m6"),
            ]
        )
    )
    creds = CredentialStore()
    creds.set_robot_password(I5, "pw-i5")
    creds.set_robot_password(M6, "pw-m6")
    return ServiceContext(
        settings=load_settings(),
        transport_factory=factory,
        session_options={"backoff_min": 0.01, "backoff_max": 0.02},
    )


async def connected(ctx: ServiceContext) -> None:
    async with asyncio.timeout(2):
        while not all(s.connection is ConnectionState.CONNECTED and s.reported for s in ctx):
            await asyncio.sleep(0.005)


@pytest.fixture
def factory() -> FakeTransportFactory:
    return FakeTransportFactory.from_fixtures(FIXTURES)


@pytest.fixture
def short_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CONFIRM_TIMEOUT_S", 0.2)


@pytest.fixture
def kit(tmp_kit_home: Path, factory: FakeTransportFactory) -> ServiceContext:
    load_operations()
    return build_kit(factory)


@pytest.fixture
async def api(kit: ServiceContext) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(kit)
    headers = {"Authorization": f"Bearer {kit.tokens.create('test')}"}
    async with running(app):
        await connected(kit)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test/v1", headers=headers
        ) as client:
            yield client


async def call(client: httpx.AsyncClient, op: str, **params: Any) -> httpx.Response:
    req = registry.http_request(registry.get(op), params)
    return await client.request(req.method, req.path, params=req.query, json=req.body)


async def test_get_settings_reads_every_supported_setting(api):
    resp = await call(api, "get_settings", robot="Vacuum")
    assert resp.status_code == 200
    assert resp.json() == {
        "passes": "one",  # noAutoPasses true, twoPass false
        "edge_clean": True,  # openOnly false
        "always_finish": False,  # binPause true
        "child_lock": False,
        "eco_charge": False,
        "schedule_hold": False,
    }
    mop = (await call(api, "get_settings", robot="Mop")).json()
    assert (mop["passes"], mop["pad_wetness"], mop["spray_overlap"]) == ("auto", 3, "standard")
    assert "always_finish" not in mop


async def test_passes_two_writes_both_keys_together(api, factory):
    resp = await call(api, "set_setting", robot="Vacuum", setting="passes", value="two")
    assert resp.status_code == 200
    assert resp.json() == {
        "robot_id": I5,
        "setting": "passes",
        "requested": "two",
        "outcome": "applied",
        "actual": "two",
    }
    assert factory.get(I5).published_settings == [{"noAutoPasses": True, "twoPass": True}]


async def test_read_back_mismatch_is_not_applied_with_actual(api, factory, short_confirm):
    # i3-style: the robot accepts the write but drops one key of the group.
    factory.get(I5).ignored_settings = {"twoPass"}
    resp = await call(api, "set_setting", robot=I5, setting="passes", value="two")
    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "not_applied"
    assert error["details"]["actual"] == "one"
    assert error["details"]["requested"] == "two"
    assert error["details"]["outcome"] == "not_applied"


async def test_dropped_single_key_write_is_not_applied(api, factory, short_confirm):
    factory.get(I5).ignored_settings = {"childLock"}
    resp = await call(api, "set_setting", robot=I5, setting="child_lock", value=True)
    assert resp.status_code == 409
    assert resp.json()["error"]["details"]["actual"] is False


async def test_pad_wetness_on_the_vacuum_is_not_supported(api, factory):
    resp = await call(api, "set_setting", robot="Vacuum", setting="pad_wetness", value=2)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "not_supported"
    assert error["details"] == {"profile": "roomba_i5", "capability": "setting.pad_wetness"}
    assert factory.get(I5).published_settings == []


@pytest.mark.parametrize(("robot", "profile"), [("Vacuum", "roomba_i5"), ("Mop", "braava_m6")])
async def test_carpet_boost_is_known_but_not_supported(api, factory, robot, profile):
    """US5 AS3: every `setting.*` capability is a known setting; a model lacking it is
    `not_supported` naming the model and capability, not `invalid_request`."""
    resp = await call(api, "set_setting", robot=robot, setting="carpet_boost", value=True)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "not_supported"
    assert error["details"] == {"profile": profile, "capability": "setting.carpet_boost"}


async def test_schedule_hold_round_trip(api, factory):
    for value in (True, False):
        resp = await call(api, "set_setting", robot="Mop", setting="schedule_hold", value=value)
        assert resp.json()["outcome"] == "applied"
        shown = await call(api, "get_settings", robot="Mop")
        assert shown.json()["schedule_hold"] is value
    assert factory.get(M6).published_settings == [{"schedHold": True}, {"schedHold": False}]


async def test_cli_string_values_are_coerced(api, factory):
    wet = await call(api, "set_setting", robot="Mop", setting="pad_wetness", value="2")
    assert wet.json()["actual"] == 2
    overlap = await call(api, "set_setting", robot="Mop", setting="spray_overlap", value="deep")
    assert overlap.json()["actual"] == "deep"
    edge = await call(api, "set_setting", robot="Mop", setting="edge_clean", value="off")
    assert edge.json()["actual"] is False
    assert factory.get(M6).published_settings == [
        {"padWetness": {"disposable": 2, "reusable": 2}},
        {"rankOverlap": 85},
        {"openOnly": True},
    ]


@pytest.mark.parametrize(
    ("setting", "value"),
    [("passes", "three"), ("pad_wetness", 4), ("pad_wetness", True), ("loudness", "max")],
)
async def test_invalid_values_and_unknown_settings(api, factory, setting, value):
    resp = await call(api, "set_setting", robot="Mop", setting=setting, value=value)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"
    assert factory.get(M6).published_settings == []


async def test_writes_queue_behind_commands(api, kit, factory):
    # FR-030: a setting write sent after a command runs only once that command is confirmed.
    fake = factory.get(I5)
    fake.reaction_delay = 0.05
    session = kit.session(I5)
    find = asyncio.create_task(session.execute("find"))
    await asyncio.sleep(0)
    change = await settings.change_setting(session, "eco_charge", True)
    assert find.done() and (await find).outcome == "confirmed"
    assert change.outcome == "applied"
    assert [name for name, _ in fake.sent_commands] == ["find"]
