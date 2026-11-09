"""US1 end to end over the real HTTP API, against fake robots and a fake cloud (T040).

Covers: discovery (supported and unsupported models), adding by address, local password
retrieval (stored, and cloud_only), inventory, every whole-home command confirmed, capability and
model errors, cloud declined, and that no password ever appears in a response.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from roomba_kit.api.app import create_app
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import RobotEntry, load_settings
from roomba_kit.core.models import DiscoveredRobot
from roomba_kit.profiles.loader import match_profile
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.operations import setup
from roomba_kit.transport.fake import FakeCloud, FakePasswordRetriever, FakeTransportFactory
from tests.unit.test_api import running

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"
ODD = "00000000000000000000000000000003"
SECRETS = ("i5-local-secret", "m6-local-secret", "account-secret")
ON_LAN = [
    (I5, "Vacuum", "i5G5020", "192.0.2.10"),
    (M6, "Mop", "m611020", "192.0.2.11"),
    (ODD, "Mystery", "x900020", "192.0.2.12"),
]


def lan_robot(rid: str, name: str, sku: str, address: str) -> DiscoveredRobot:
    profile = match_profile(sku)
    return DiscoveredRobot(
        id=rid,
        name=name,
        sku=sku,
        firmware="fw+1.0",
        address=address,
        supported=profile is not None,
        profile_id=profile.id if profile else None,
    )


async def discover(broadcast: str, timeout_s: float) -> list[DiscoveredRobot]:
    return [lan_robot(*r) for r in ON_LAN]


async def probe(address: str, timeout_s: float) -> DiscoveredRobot | None:
    return next((lan_robot(*r) for r in ON_LAN if r[3] == address), None)


@pytest.fixture
def factory() -> FakeTransportFactory:
    return FakeTransportFactory.from_fixtures(FIXTURES)


@pytest.fixture
def retriever() -> FakePasswordRetriever:
    return FakePasswordRetriever({"192.0.2.10": SECRETS[0], "192.0.2.11": SECRETS[1]})


@pytest.fixture
async def ctx(tmp_kit_home, factory, retriever) -> ServiceContext:
    return ServiceContext(
        settings=load_settings(),
        transport_factory=factory,
        password_retriever=retriever,
        cloud_factory=lambda username, password, country: FakeCloud(),
        discover=discover,
        probe=probe,
        session_options={"backoff_min": 0.01, "backoff_max": 0.02},
    )


@pytest.fixture
async def api(ctx) -> AsyncIterator[Any]:
    """`api(method, path, **kw)` against the running app; every body is kept for leak checks."""
    bodies: list[bytes] = []
    app = create_app(ctx)

    async def keep(response: httpx.Response) -> None:
        bodies.append(await response.aread())

    async with running(app):
        token = ctx.tokens.create("cli")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test/v1",
            headers={"Authorization": f"Bearer {token}"},
            event_hooks={"response": [keep]},
        ) as client:

            async def call(method: str, path: str, **kw: Any) -> httpx.Response:
                return await client.request(method, path, **kw)

            call.bodies = bodies  # type: ignore[attr-defined]
            call.client = client  # type: ignore[attr-defined]
            yield call
    for body in bodies:
        for secret in SECRETS:
            assert secret.encode() not in body


async def wait_connected(api, robot: str) -> None:
    async with asyncio.timeout(3):
        while (await api("GET", f"/robots/{robot}")).json()["connection"] != "connected":
            await asyncio.sleep(0.01)


@pytest.fixture
async def configured(ctx, api) -> None:
    """Both fixture robots plus an unsupported model, the way setup leaves them."""
    for rid, name, address, sku in [
        (I5, "Vacuum", "192.0.2.10", "i5G5020"),
        (M6, "Mop", "192.0.2.11", "m611020"),
        (ODD, "Mystery", "192.0.2.12", "x900020"),
    ]:
        await ctx.add_robot(RobotEntry(id=rid, name=name, address=address, sku=sku))
    for robot, secret in ((I5, SECRETS[0]), (M6, SECRETS[1])):
        stored = await api("POST", f"/robots/{robot}/password", json={"password": secret})
        assert stored.json() == {"outcome": "stored"}
    await wait_connected(api, I5)
    await wait_connected(api, M6)


async def test_inventory_lists_supported_and_unsupported_robots(api, configured):
    robots = {r["id"]: r for r in (await api("GET", "/robots")).json()}
    assert set(robots) == {I5, M6, ODD}
    i5 = robots[I5]
    assert (i5["name"], i5["profile_id"], i5["supported"], i5["sku"]) == (
        "Vacuum",
        "roomba_i5",
        True,
        "i5G5020",
    )
    assert i5["address"] == "192.0.2.10" and i5["firmware"] and i5["connection"] == "connected"
    caps = {c["capability"]: c["verification"]["status"] for c in i5["capabilities"]}
    assert {"clean", "dock", "evac"} <= set(caps)
    assert robots[ODD]["supported"] is False and robots[ODD]["capabilities"] == []
    assert robots[ODD]["profile_id"] is None and robots[ODD]["sku"] == "x900020"

    by_name = await api("GET", "/robots/mop/capabilities")
    assert "evac" not in {c["capability"] for c in by_name.json()}
    missing = await api("GET", "/robots/nobody")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "robot_not_found"


async def test_whole_home_commands_are_confirmed(api, configured, factory):
    async def command(name: str) -> dict[str, Any]:
        resp = await api("POST", f"/robots/Vacuum/commands/{name}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["robot_id"] == I5 and body["command"] == name
        assert body["outcome"] == "confirmed", body
        return body

    assert (await command("clean"))["state"]["activity"] == "cleaning"
    assert (await command("pause"))["state"]["activity"] == "paused"
    assert (await command("resume"))["state"]["activity"] == "cleaning"

    fake = factory.get(I5)

    async def robot_ends_mission() -> None:  # the fake doesn't act on `stop`; the robot would
        while not any(name == "stop" for name, _ in fake.sent_commands):
            await asyncio.sleep(0.005)
        fake.emit({"cleanMissionStatus": {**fake.reported["cleanMissionStatus"], "cycle": "none"}})

    ender = asyncio.create_task(robot_ends_mission())
    assert (await command("stop"))["state"]["mission"] is None
    await ender
    assert (await command("dock"))["state"]["activity"] in {"returning", "docked", "charging"}
    await command("find")
    sent = [name for name, _ in fake.sent_commands]
    assert sent == ["start", "pause", "resume", "stop", "dock", "find"]


async def test_capability_and_model_errors(api, configured, factory):
    evac = await api("POST", f"/robots/{M6}/commands/evac")
    assert evac.status_code == 422
    assert evac.json()["error"]["details"] == {"profile": "braava_m6", "capability": "evac"}
    assert factory.get(M6).sent_commands == []

    odd = await api("POST", f"/robots/{ODD}/commands/clean")
    assert odd.status_code == 422 and odd.json()["error"]["code"] == "unsupported_model"


async def test_cloud_declined_leaves_local_control_working(api, configured):
    status = (await api("GET", "/cloud_status")).json()
    assert status["status"] == "not_configured"
    service = (await api("GET", "/service_status")).json()
    assert service["cloud"]["status"] == "not_configured"
    assert {r["id"]: r["connection"] for r in service["robots"]}[I5] == "connected"
    clean = await api("POST", f"/robots/{I5}/commands/clean")
    assert clean.json()["outcome"] == "confirmed"
    pw = await api("POST", f"/robots/{I5}/password/cloud")
    assert pw.json() == {"outcome": "unavailable", "reason": "not_configured"}


async def test_health_needs_no_token_and_config_show_redacts(api, configured):
    health = await api.client.get("/health", headers={"Authorization": ""})
    assert health.status_code == 200 and health.json()["status"] == "ok"
    assert (await api.client.get("/robots", headers={"Authorization": ""})).status_code == 401

    CredentialStore().set_cloud("someone@example.com", SECRETS[2])
    shown = (await api("GET", "/config_show")).json()
    assert shown["cloud"]["username"] == "***" and shown["cloud"]["password"] == "***"
    passwords = {r["id"]: r["password"] for r in shown["robots"]}
    assert passwords == {I5: "***", M6: "***", ODD: None}
    assert shown["tokens"] == [{"name": "cli", "created_at": shown["tokens"][0]["created_at"]}]
    assert "sha256" not in str(shown) and "someone@example.com" not in str(shown)


async def test_setup_from_zero(api, factory, monkeypatch):
    monkeypatch.setattr(setup, "RETRY_S", 0.01)
    found = {r["id"]: r for r in (await api("GET", "/discover_robots")).json()}
    assert {rid: r["supported"] for rid, r in found.items()} == {I5: True, M6: True, ODD: False}
    assert found[I5]["profile_id"] == "roomba_i5" and not found[I5]["configured"]
    assert found[ODD]["profile_id"] is None and found[ODD]["sku"] == "x900020"
    bad = await api("GET", "/discover_robots", params={"broadcast": "not-an-ip"})
    assert bad.status_code == 400

    added = await api("POST", "/robots", json={"address": "192.0.2.10"})
    assert added.json()["id"] == I5 and added.json()["configured"] is True
    nobody = await api("POST", "/robots", json={"address": "192.0.2.99"})
    assert nobody.json()["error"]["code"] == "robot_offline"

    pairing = await api("POST", "/robots/Vacuum/password/retrieve", json={"timeout_s": 5})
    assert pairing.json() == {"outcome": "stored", "steps": setup.PASSWORD_STEPS}
    assert CredentialStore().robot_password(I5) == SECRETS[0]
    await wait_connected(api, I5)
    assert factory.get(I5).password == SECRETS[0]
    assert (await api("POST", f"/robots/{I5}/commands/clean")).json()["outcome"] == "confirmed"
    found = {r["id"]: r for r in (await api("GET", "/discover_robots")).json()}
    assert found[I5]["configured"] and not found[M6]["configured"]


async def test_robot_that_wont_release_its_password_is_cloud_only(api, retriever, monkeypatch):
    monkeypatch.setattr(setup, "RETRY_S", 0.01)
    del retriever.passwords["192.0.2.11"]
    await api("POST", "/robots", json={"address": "192.0.2.11"})
    pairing = await api("POST", f"/robots/{M6}/password/retrieve", json={"timeout_s": 0.2})
    assert pairing.json()["outcome"] == "cloud_only"
    assert len(retriever.calls) > 1  # retried through the window, not given up at once
    assert CredentialStore().robot_password(M6) is None


async def test_unanswered_retrieval_times_out(api, retriever, monkeypatch):
    monkeypatch.setattr(setup, "RETRY_S", 0.01)
    retriever.delay = 1.0
    await api("POST", "/robots", json={"address": "192.0.2.10"})
    pairing = await api("POST", f"/robots/{I5}/password/retrieve", json={"timeout_s": 0.1})
    assert pairing.json()["outcome"] == "timeout"


async def test_remove_robot(api, configured):
    assert (await api("DELETE", "/robots/Mop")).json() == {"removed": True}
    assert {r["id"] for r in (await api("GET", "/robots")).json()} == {I5, ODD}
    assert CredentialStore().robot_password(M6) is None
    assert CredentialStore().robot_password(I5) == SECRETS[0]
    assert (await api("DELETE", "/robots/Mop")).json() == {"removed": False}
