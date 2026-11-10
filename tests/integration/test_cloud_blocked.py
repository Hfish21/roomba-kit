"""With the cloud configured but blocked, local control works.

Two cloud failure modes: every call fails at once (`fail`), and every call hangs forever (`hang`,
a black-holed connection). The US1, US2, US3 (local room names), US5 and US8 suites are rerun
unchanged under each mode by a pytest plugin (`tests/integration/helpers.py`) that gives every
`ServiceContext` configured cloud credentials and a blocked cloud; the plugin's report proves the
cloud really was attempted and unavailable in every test. The in-process tests below add the
latency bounds and check every cloud operation's `cloud_unavailable` answer.
"""

import asyncio
import json
import os
import sys
import time
import xml.etree.ElementTree as ET  # noqa: S405 - parsing our own junit report
from pathlib import Path
from typing import Any

import pytest

from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import load_settings
from roomba_kit.service import cloud
from roomba_kit.service.app import ServiceContext
from roomba_kit.transport.fake import FakeCloud, FakeTransportFactory
from tests.integration.helpers import (
    CLOUD_MODE_ENV,
    FAST_BACKOFF,
    FIXTURES,
    I5,
    M6,
    REPORT_ENV,
    api_client,
    blocked_cloud,
    cloud_unavailable_reason,
    configure_robots,
    http_call,
    serving,
    until,
)

REPO = Path(__file__).parents[2]
SUITES = [
    "tests/integration/test_us1_setup_and_commands.py",
    "tests/integration/test_us2_state_events.py",
    "tests/integration/test_us3_rooms.py::test_learned_rooms_are_captured_and_named_without_the_cloud",
    "tests/integration/test_us5_settings.py",
    "tests/integration/test_us8_history.py",
]
DESELECTED = {
    # Asserts the *never configured* cloud state; its blocked counterpart is
    # `test_local_ops_stay_fast_and_cloud_ops_say_unavailable` below.
    "tests/integration/test_us1_setup_and_commands.py::test_cloud_declined_leaves_local_control_working",
}
MIN_TESTS = 30
LOCAL_BOUND_S = 0.5
"""Per local operation, in-process against fakes. They take a few ms; a cloud wait would be ≥ s."""


async def run_suites(mode: str, tmp: Path) -> tuple[dict[str, int], list[dict[str, Any]]]:
    junit, report = tmp / f"{mode}.xml", tmp / f"{mode}.jsonl"
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    args += ["-p", "tests.integration.helpers", f"--junitxml={junit}", *SUITES]
    for nodeid in DESELECTED:
        args += ["--deselect", nodeid]
    env = {**os.environ, CLOUD_MODE_ENV: mode, REPORT_ENV: str(report)}
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=REPO, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await asyncio.wait_for(proc.communicate(), 120)
    assert proc.returncode == 0, out.decode()[-4000:]
    suite = ET.parse(junit).getroot().find("testsuite")  # noqa: S314
    assert suite is not None
    counts = {k: int(suite.get(k, 0)) for k in ("tests", "failures", "errors", "skipped")}
    rows = [json.loads(line) for line in report.read_text().splitlines()]
    return counts, rows


async def test_story_suites_pass_with_the_cloud_blocked(tmp_path):
    results = await asyncio.gather(*(run_suites(mode, tmp_path) for mode in ("fail", "hang")))
    for mode, (counts, rows) in zip(("fail", "hang"), results, strict=True):
        assert counts["tests"] >= MIN_TESTS, (mode, counts)
        assert counts["failures"] == counts["errors"] == counts["skipped"] == 0, (mode, counts)
        assert len(rows) == counts["tests"], "the plugin must have run for every test"
        for row in rows:
            started = [c for c in row["contexts"] if c["calls"]]
            # The cloud was configured, attempted and unavailable throughout: not vacuous.
            assert started, f"{mode}: no service attempted the cloud in {row['test']}"
            for context in started:
                assert all(calls == ["login"] for calls in context["calls"]), (mode, row)
                assert context["cloud"]["status"] == "unavailable", (mode, row)
                assert context["cloud"]["last_ok"] is None, (mode, row)


@pytest.fixture(params=["fail", "hang"])
def mode(request) -> str:
    return str(request.param)


@pytest.fixture
async def kit(tmp_kit_home, mode, monkeypatch):
    monkeypatch.setattr(cloud, "LOGIN_TIMEOUT_S", 0.3)
    configure_robots()
    CredentialStore().set_cloud("owner@example.com", "cloud-pw")
    factory = FakeTransportFactory.from_fixtures(FIXTURES)
    clouds: list[FakeCloud] = []

    def cloud_factory(username: str, password: str, country: str) -> FakeCloud:
        clouds.append(blocked_cloud(mode))
        return clouds[-1]

    ctx = ServiceContext(
        settings=load_settings(),
        transport_factory=factory,
        cloud_factory=cloud_factory,
        session_options=FAST_BACKOFF,
    )
    async with serving(ctx) as app, api_client(app, ctx.tokens.create("test")) as client:
        yield ctx, client, factory, clouds


async def timed(client, op: str, **params: Any) -> tuple[Any, float]:
    started = time.monotonic()
    response = await http_call(client, op, params)
    return response, time.monotonic() - started


async def test_local_ops_stay_fast_and_cloud_ops_say_unavailable(kit, mode):
    ctx, client, factory, clouds = kit
    expected_reason = {"fail": "connection_failed", "hang": "timeout"}[mode]
    await until(lambda: ctx.cloud.status().reason == expected_reason)
    assert clouds and clouds[0].calls[0] == "login"

    # A login hanging in the background while local commands run.
    pending = asyncio.create_task(
        http_call(client, "cloud_login", {"username": "owner@example.com", "password": "pw"})
    )
    local = [
        ("health", {}, 200),
        ("service_status", {}, 200),
        ("list_robots", {}, 200),
        ("get_state", {"robot": "Vacuum"}, 200),
        ("get_settings", {"robot": "Mop"}, 200),
        ("set_setting", {"robot": "Vacuum", "setting": "passes", "value": "two"}, 200),
        ("clean", {"robot": "Vacuum"}, 200),
        ("pause", {"robot": "Vacuum"}, 200),
        ("resume", {"robot": "Vacuum"}, 200),
        ("dock", {"robot": "Vacuum"}, 200),
        ("find", {"robot": "Mop"}, 200),
        ("get_position", {"robot": "Mop"}, 200),
        ("list_rooms", {"robot": "Vacuum"}, 200),
        ("events_list", {"since": 0}, 200),
        ("list_missions", {}, 200),
        ("config_show", {}, 200),
    ]
    for op, params, status in local:
        response, took = await timed(client, op, **params)
        assert response.status_code == status, (op, response.text)
        assert took < LOCAL_BOUND_S, (op, took)
        if op in {"clean", "pause", "resume", "dock", "find"}:
            assert response.json()["outcome"] == "confirmed", (op, response.json())

    reasons = {}
    for op, params in [
        ("import_rooms", {"robot": I5}),
        ("get_floor_plan", {"robot": I5}),
        ("cloud_history", {"robot": M6}),
        ("get_coverage", {"robot": I5, "mission_id": "01M0J6GSQ8MB898573DF8CCQNN"}),
    ]:
        response, took = await timed(client, op, **params)
        reasons[op] = cloud_unavailable_reason(response)
        assert took < LOCAL_BOUND_S, (op, took)
    assert set(reasons.values()) == {expected_reason}, reasons

    response, took = await timed(client, "cloud_retrieve_password", robot="Mop")
    assert response.json() == {"outcome": "unavailable", "reason": expected_reason}
    assert took < LOCAL_BOUND_S
    status = (await http_call(client, "cloud_status", {})).json()
    assert (status["status"], status["reason"]) == ("unavailable", expected_reason)

    login = (await pending).json()
    assert login == {"status": "failed", "reason": expected_reason}
    assert factory.get(I5).sent_commands  # the commands really went to the robot
    assert all(set(c.calls) <= {"login"} for c in clouds), [c.calls for c in clouds]
