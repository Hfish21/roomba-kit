"""T074: `verify` against the fake robots."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import Activity, PositionFix
from roomba_kit.service import confirm, settings
from roomba_kit.service import verify as checks
from roomba_kit.service.app import ServiceContext
from roomba_kit.transport.fake import FakeCloud, FakeTransportFactory
from tests.integration.test_us5_settings import FIXTURES, I5, M6, build_kit, connected

I5_SETTINGS = ["passes", "edge_clean", "always_finish", "child_lock", "eco_charge", "schedule_hold"]


@pytest.fixture
def factory() -> FakeTransportFactory:
    return FakeTransportFactory.from_fixtures(FIXTURES)


@pytest.fixture
async def kit(tmp_kit_home: Path, factory: FakeTransportFactory) -> AsyncIterator[ServiceContext]:
    ctx = build_kit(factory)
    await ctx.start()
    try:
        await connected(ctx)
        yield ctx
    finally:
        await ctx.stop()


async def verify(ctx: ServiceContext, robot, capabilities=None, allow_motion=False):
    return await checks.verify(ctx, ctx.resolve_robot(robot), capabilities, allow_motion)


def by_cap(run) -> dict[str, tuple[str, str]]:
    return {r.capability: (r.result, r.evidence) for r in run.results}


async def test_default_run_without_motion(kit, factory):
    fake = factory.get(I5)
    before = {k: fake.reported[k] for k in ("noAutoPasses", "twoPass", "openOnly", "binPause")}
    run = await verify(kit, robot="Vacuum")
    results = by_cap(run)
    assert set(results) == {c.value for c in kit.session(I5).profile.capabilities}
    for name in I5_SETTINGS:
        assert results[f"setting.{name}"][0] == "pass", results[f"setting.{name}"]
    assert results["setting.passes"][1].startswith("one → two: applied")
    assert {k: fake.reported[k] for k in before} == before  # every setting restored
    assert len(fake.published_settings) == 2 * len(I5_SETTINGS)
    assert results["find"][0] == "pass"
    assert results["state.bin"] == ("pass", 'reported {"present": true, "full": false}')
    assert results["cloud.rooms"] == ("skipped", "cloud unavailable (not_configured)")
    assert results["train"][0] == "skipped" and "named explicitly" in results["train"][1]
    for cap in ("clean", "pause", "resume", "stop", "dock", "evac", "reboot", "clean_rooms"):
        assert results[cap][0] == "skipped", cap
    assert [name for name, _ in fake.sent_commands] == ["find"]  # nothing moved


async def test_run_is_stored_and_reported(kit):
    run = await verify(kit, robot=I5, capabilities=["setting.child_lock", "clean"])
    row = await kit.store.fetchone("SELECT * FROM verification_runs WHERE id = ?", (run.id,))
    assert (row["robot_id"], row["profile_id"], row["allow_motion"]) == (I5, "roomba_i5", 0)
    assert [r["capability"] for r in json.loads(row["results"])] == ["setting.child_lock", "clean"]
    assert run.anchor == f"run-{run.started_at:%Y%m%d-%H%M%S}"
    assert f'<a id="{run.anchor}"></a>' in run.report
    assert f"docs/verification/roomba_i5.md#{run.anchor}" in run.report
    assert "| `setting.child_lock` | pass |" in run.report
    assert "1 pass, 0 fail, 1 skipped" in run.report
    assert I5 not in run.report and "Vacuum" not in run.report and "192.0.2" not in run.report


async def test_dropped_write_fails_with_evidence(kit, factory, monkeypatch):
    monkeypatch.setattr(settings, "CONFIRM_TIMEOUT_S", 0.1)
    factory.get(I5).ignored_settings = {"twoPass"}
    run = await verify(kit, robot=I5, capabilities=["setting.passes", "setting.child_lock"])
    result, evidence = by_cap(run)["setting.passes"]
    assert result == "fail"
    assert "one → two: not_applied (read back one)" in evidence
    assert by_cap(run)["setting.child_lock"][0] == "pass"


async def test_failed_restore_is_flagged(kit, factory, monkeypatch):
    monkeypatch.setattr(settings, "CONFIRM_TIMEOUT_S", 0.1)
    fake = factory.get(I5)
    original = fake.publish_settings

    async def apply_first_only(values):  # the write lands, the restore is dropped
        fake.ignored_settings = set(values) if fake.published_settings else set()
        await original(values)

    fake.publish_settings = apply_first_only
    run = await verify(kit, robot=I5, capabilities=["setting.eco_charge"])
    result, evidence = by_cap(run)["setting.eco_charge"]
    assert result == "fail" and evidence.startswith("RESTORE FAILED")


@pytest.mark.parametrize(
    ("capabilities", "code"),
    [
        (["setting.passes", "setting.pad_wetness"], ErrorCode.NOT_SUPPORTED),
        (["warp_drive"], ErrorCode.INVALID_REQUEST),
        ([], ErrorCode.INVALID_REQUEST),  # finding 9: an empty list is not "everything"
    ],
)
async def test_9_refused_before_any_robot_contact(kit, factory, capabilities, code):
    with pytest.raises(KitError) as exc:
        await verify(kit, robot=I5, capabilities=capabilities)
    assert exc.value.code is code
    assert factory.get(I5).published_settings == [] and factory.get(I5).sent_commands == []


async def test_train_needs_explicit_name_and_motion(kit, factory, monkeypatch):
    fake = factory.get(I5)
    short(monkeypatch, "stop", "reboot", "train")  # the fake never acts on these
    named = await verify(kit, robot=I5, capabilities=["train"])
    assert by_cap(named)["train"][0] == "skipped"
    everything = await verify(kit, robot=I5, capabilities=["reboot", "evac"], allow_motion=False)
    assert {r.result for r in everything.results} == {"skipped"}
    assert fake.sent_commands == []
    default = await verify(kit, robot=I5, capabilities=None, allow_motion=True)
    assert by_cap(default)["train"][0] == "skipped"
    assert "train" not in [name for name, _ in fake.sent_commands]
    fake.sent_commands.clear()
    await verify(kit, robot=I5, capabilities=["train"], allow_motion=True)
    assert [name for name, _ in fake.sent_commands] == ["train"]


async def test_motion_sequence_starts_from_and_returns_to_the_dock(kit, factory):
    fake = factory.get(I5)
    run = await verify(kit, robot=I5, capabilities=["resume", "find"], allow_motion=True)
    assert [name for name, _ in fake.sent_commands] == ["find", "start", "pause", "resume", "dock"]
    assert [(r.capability, r.result) for r in run.results] == [("find", "pass"), ("resume", "pass")]


async def test_motion_failure_skips_dependents_but_still_docks(kit, factory, monkeypatch):
    fake = factory.get(I5)
    fake.dropped_commands = {"pause"}
    short(monkeypatch, "pause")
    run = await verify(kit, robot=I5, capabilities=["pause", "resume", "stop"], allow_motion=True)
    results = by_cap(run)
    assert results["pause"][0] == "fail"
    assert results["resume"] == ("skipped", "not run: pause didn't pass")
    assert results["stop"][0] == "skipped"
    assert [name for name, _ in fake.sent_commands] == ["start", "pause", "dock"]


async def test_motion_checks_need_the_robot_docked(kit, factory):
    fake = factory.get(I5)
    fake.emit({"cleanMissionStatus": {"cycle": "clean", "phase": "run"}})
    await until(lambda: kit.session(I5).state.activity is Activity.CLEANING)
    run = await verify(kit, robot=I5, capabilities=["clean", "dock"], allow_motion=True)
    assert {r.result for r in run.results} == {"skipped"}
    assert fake.sent_commands == []


async def until(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def short(monkeypatch, *commands, window=0.05):
    for command in commands:
        monkeypatch.setitem(
            confirm.DEFAULT_SPECS, command, confirm.CommandSpec(lambda v: False, window)
        )


def record_activity_on_send(kit, fake):
    """`[(command, activity when it was sent)]`."""
    seen, send = [], fake.send_command

    async def recording(name, params=None):
        seen.append((name, kit.session(fake.robot_id).state.activity))
        await send(name, params)

    fake.send_command = recording
    return seen


async def test_1_reboot_waits_for_the_robot_to_be_back_on_the_dock(kit, factory, monkeypatch):
    short(monkeypatch, "reboot")
    fake = factory.get(I5)
    fake.dock_delay = 0.2  # dock confirms on `returning`; docked 0.2 s later
    seen = record_activity_on_send(kit, fake)
    await verify(kit, robot=I5, capabilities=["clean", "reboot"], allow_motion=True)
    assert [n for n, _ in seen] == ["start", "dock", "reboot"]
    assert seen[-1][1] in (Activity.DOCKED, Activity.CHARGING)


async def test_1_reboot_and_train_skipped_unless_docked(kit, factory, monkeypatch):
    monkeypatch.setattr(checks, "SETTLE_S", 0.05, raising=False)
    short(monkeypatch, "reboot", "train")
    fake = factory.get(I5)
    fake.dock_delay = 1.0
    run = await verify(kit, robot=I5, capabilities=["clean", "reboot", "train"], allow_motion=True)
    assert by_cap(run)["reboot"][0] == "skipped" and by_cap(run)["train"][0] == "skipped"
    assert "WARNING: the robot wasn't back on its dock" in by_cap(run)["clean"][1]
    assert [n for n, _ in fake.sent_commands] == ["start", "dock"]


async def test_2_dock_does_not_pass_when_the_robot_never_left(kit, factory, monkeypatch):
    short(monkeypatch, "clean")
    fake = factory.get(I5)
    fake.dropped_commands = {"start"}
    run = await verify(kit, robot=I5, capabilities=["clean", "dock"], allow_motion=True)
    assert by_cap(run)["clean"][0] == "fail"
    assert by_cap(run)["dock"] == ("skipped", "not checked: clean didn't pass")
    assert [n for n, _ in fake.sent_commands] == ["start"]  # docked all along: no safety dock


async def test_3_cancelled_setting_check_still_restores(kit, factory):
    fake = factory.get(I5)
    fake.reaction_delay = 0.1
    task = asyncio.create_task(verify(kit, robot=I5, capabilities=["setting.eco_charge"]))
    await until(lambda: fake.published_settings)
    task.cancel()
    await until(lambda: len(fake.published_settings) == 2)
    await fake.settle()
    assert fake.published_settings == [{"ecoCharge": True}, {"ecoCharge": False}]
    assert fake.reported["ecoCharge"] is False


async def test_3_cancelled_motion_check_still_docks(kit, factory):
    fake = factory.get(I5)
    fake.reaction_delay = 0.05
    task = asyncio.create_task(
        verify(kit, robot=I5, capabilities=["clean", "pause"], allow_motion=True)
    )
    await until(lambda: kit.session(I5).state.activity is Activity.CLEANING)
    task.cancel()
    await until(lambda: "dock" in [n for n, _ in fake.sent_commands])
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(("raises_on", "flag"), [(2, "RESTORE FAILED"), (1, "False → True: error")])
async def test_4_restore_always_attempted_and_errors_flagged(kit, factory, raises_on, flag):
    fake = factory.get(I5)
    publish = fake.publish_settings

    async def raise_after_publishing(values):  # 1: the write fails, 2: the restore fails
        await publish(values)
        if len(fake.published_settings) == raises_on:
            raise KitError(ErrorCode.ROBOT_OFFLINE, "link lost")

    fake.publish_settings = raise_after_publishing
    result, evidence = by_cap(await verify(kit, robot=I5, capabilities=["setting.child_lock"]))[
        "setting.child_lock"
    ]
    assert result == "fail" and flag in evidence and "error robot_offline" in evidence
    assert fake.published_settings == [{"childLock": True}, {"childLock": False}]


async def test_5_motion_runs_after_evac_once_the_robot_is_docked(kit, factory):
    fake = factory.get(I5)
    fake.evac_duration = 0.1  # still `emptying_bin` when evac is confirmed
    run = await verify(kit, robot=I5, capabilities=["evac", "clean"], allow_motion=True)
    assert by_cap(run)["evac"][0] == "pass" and by_cap(run)["clean"][0] == "pass"
    assert [n for n, _ in fake.sent_commands] == ["evac", "start", "dock"]


async def test_6_clean_base_needs_a_reported_state(kit, factory):
    fake = factory.get(I5)
    fake.emit({"dock": {"known": True, "state": None}})  # a plain Home Base
    await until(lambda: kit.session(I5).state.clean_base.state is None)
    run = await verify(kit, robot=I5, capabilities=["state.clean_base"])
    assert by_cap(run)["state.clean_base"][0] == "fail"


async def test_7_empty_cloud_history_fails(kit):
    kit.cloud = SimpleNamespace(transport=lambda: FakeCloud())
    run = await verify(kit, robot=I5, capabilities=["cloud.history", "cloud.rooms"])
    assert by_cap(run)["cloud.history"] == ("fail", "the cloud returned no missions")
    kit.cloud = SimpleNamespace(transport=lambda: FakeCloud.from_fixtures(FIXTURES))
    again = await verify(kit, robot=I5, capabilities=["cloud.history"])
    assert by_cap(again)["cloud.history"][0] == "pass"


async def test_8_live_position_needs_a_fix(kit, factory):
    fake = factory.get(M6)
    fake.script_positions(None)
    docked = await verify(kit, robot="Mop", capabilities=["live_position"])
    assert by_cap(docked)["live_position"][0] == "skipped"
    fix = PositionFix(robot_id=M6, at=docked.started_at, x_m=1, y_m=2, heading_rad=0)
    fake.script_positions(None, fix)  # no fix on the dock, a fix while cleaning
    run = await verify(kit, robot="Mop", capabilities=["live_position", "clean"], allow_motion=True)
    assert by_cap(run)["live_position"] == ("pass", "fix at (1.00, 2.00) m")


async def test_10_passes_restore_keeps_the_latent_two_pass_key(kit, factory):
    fake = factory.get(I5)
    fake.emit({"noAutoPasses": False, "twoPass": True})  # reads `auto`, twoPass latent
    await until(lambda: kit.session(I5).reported.get("twoPass") is True)
    run = await verify(kit, robot=I5, capabilities=["setting.passes"])
    assert by_cap(run)["setting.passes"][0] == "pass"
    await fake.settle()
    assert (fake.reported["noAutoPasses"], fake.reported["twoPass"]) == (False, True)
