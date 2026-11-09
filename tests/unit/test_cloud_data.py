"""The cloud timeline parser and the watchers' restart rules (T078)."""

import asyncio
import json
from pathlib import Path

import pytest

from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import ConfigModel, RobotEntry, load_settings, save_config
from roomba_kit.service import cloud, cloud_data
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.cloud_data import TimelineParser
from roomba_kit.transport.fake import FakeCloud, FakeTransportFactory

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"


def reports() -> list[dict]:
    lines = (FIXTURES / "i5_timeline.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line]


def kinds(parser: TimelineParser, report: dict) -> list[str]:
    return [d.raw_type for d in parser.feed(report)]


def test_first_report_mid_mission_does_not_replay_finished_entries():
    parser = TimelineParser()
    assert kinds(parser, reports()[25]) == ["travel", "traversal"]  # current only, oldest first
    assert kinds(parser, reports()[26]) == []
    assert kinds(parser, reports()[27]) == ["evac"]


def test_entries_missed_between_reports_come_from_fin_events():
    parser = TimelineParser()
    all_reports = reports()
    assert kinds(parser, all_reports[0]) == ["start"]
    # Reports 5–28 lost (say, a reconnect): fin brings travel, room, travel, traversal, evac.
    assert kinds(parser, all_reports[29]) == [
        "travel",
        "room",
        "travel",
        "traversal",
        "evac",
        "fin",
    ]


def test_a_new_mission_resets_and_malformed_entries_are_skipped():
    parser = TimelineParser()
    parser.feed(reports()[0])
    report = {"mission_id": "M2", "event": [{"ts": 1}, "junk", {"type": "start", "ts": 2}]}
    assert [(d.mission_id, d.kind) for d in parser.feed(report)] == [("M2", "start")]
    assert parser.feed({"event": None, "finEvents": None}) == []


class Accounts:
    """A cloud factory making a fresh FakeCloud per login, so a new login is a new transport."""

    def __init__(self) -> None:
        self.made: list[FakeCloud] = []

    def __call__(self, username: str, password: str, country: str) -> FakeCloud:
        fake = FakeCloud.from_fixtures(FIXTURES)
        self.made.append(fake)
        return fake


@pytest.fixture
async def ctx(tmp_kit_home, monkeypatch):
    monkeypatch.setattr(cloud, "POLL_S", 0.02)
    monkeypatch.setattr(cloud_data, "RECONCILE_S", 0.02)
    save_config(
        ConfigModel(
            robots=[RobotEntry(id=I5, name="Vacuum", address="192.0.2.10", profile="roomba_i5")]
        )
    )
    CredentialStore().set_robot_password(I5, "pw1")
    CredentialStore().set_cloud("owner@example.com", "first")
    accounts = Accounts()
    c = ServiceContext(
        settings=load_settings(),
        transport_factory=FakeTransportFactory.from_fixtures(FIXTURES),
        cloud_factory=accounts,
        session_options={"backoff_min": 0.01, "backoff_max": 0.02},
    )
    c.accounts = accounts
    await c.start()
    yield c
    await c.stop()


async def until(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def test_watchers_rebind_when_the_login_changes(ctx):
    watchers = cloud_data.timeline_watchers(ctx)
    await until(lambda: watchers.watching() == {I5})
    first = ctx.cloud.transport()
    CredentialStore().set_cloud("owner@example.com", "second")
    await until(lambda: ctx.cloud.transport() is not first)
    await until(lambda: watchers._tasks.get(I5, (None,))[0] is ctx.cloud.transport())
    assert watchers.watching() == {I5}
