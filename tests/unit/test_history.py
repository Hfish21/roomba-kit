from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from roomba_kit.core.events import (
    ActivityChangedData,
    ErrorEventData,
    EventType,
    MissionEventData,
    encode_data,
)
from roomba_kit.core.models import CodeText, Initiator, Mission, MissionOutcome
from roomba_kit.profiles.roomba_i5 import PROFILE as I5_PROFILE
from roomba_kit.service.derive import Deriver
from roomba_kit.service.eventbus import EventBus
from roomba_kit.service.history import History, cloud_ref, list_missions
from roomba_kit.service.normalize import merge, normalize
from roomba_kit.service.store import Store
from roomba_kit.transport.fake import FakeCloud

I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"
T0 = datetime(2026, 11, 8, 12, tzinfo=UTC)
JAM = CodeText(code=1, text="Left wheel off floor")


def mission(mid: str = "M1", robot: str = I5, **fields) -> Mission:
    return Mission(id=mid, robot_id=robot, started_at=T0, initiator=Initiator.KIT, **fields)


@pytest.fixture
async def store(tmp_kit_home: Path) -> AsyncIterator[Store]:
    async with Store() as s:
        yield s


@pytest.fixture
async def bus(store: Store) -> AsyncIterator[EventBus]:
    b = EventBus(store)
    await b.start()
    yield b
    await b.close()


async def publish_mission(bus: EventBus, type_: EventType, m: Mission) -> None:
    await bus.publish(type_, m.robot_id, MissionEventData(mission=m), at=T0)


async def test_events_persist_missions_transitions_and_errors(store, bus) -> None:
    h = History(store)
    await publish_mission(bus, EventType.MISSION_STARTED, mission(rooms_requested=["7"]))
    await bus.publish(
        EventType.ACTIVITY_CHANGED,
        I5,
        ActivityChangedData.model_validate(
            {"from": "docked", "to": "cleaning", "mission_id": "M1"}
        ),
    )
    for kind in (
        "error",
        "error",
        "not_ready",
    ):  # a repeat isn't appended twice; not_ready isn't one
        await bus.publish(EventType.ERROR_RAISED, I5, ErrorEventData(kind=kind, **JAM.model_dump()))
    await h.catch_up(bus)
    (open_,) = await list_missions(store)
    assert open_.outcome is MissionOutcome.IN_PROGRESS and open_.errors == [JAM]

    done = mission(rooms_requested=["7"], outcome="completed", ended_at=T0 + timedelta(minutes=5))
    done.duration_s = 300
    await publish_mission(bus, EventType.MISSION_FINISHED, done)
    await h.catch_up(bus)
    (row,) = await list_missions(store)
    assert row.outcome is MissionOutcome.COMPLETED and row.duration_s == 300
    assert row.errors == [JAM] and row.rooms_requested == ["7"]  # errors merged, not replaced
    rows = await store.fetchall("SELECT * FROM activity_transitions")
    assert [(r["from_activity"], r["to_activity"], r["mission_id"]) for r in rows] == [
        ("docked", "cleaning", "M1")
    ]


async def test_cursor_makes_replay_idempotent_and_starts_never_reopen(store, bus) -> None:
    h = History(store)
    await publish_mission(bus, EventType.MISSION_STARTED, mission())
    await publish_mission(bus, EventType.MISSION_FINISHED, mission(outcome="cancelled"))
    assert await h.catch_up(bus) == 2
    assert await h.catch_up(bus) == 0
    assert await History(store).catch_up(bus) == 0  # the cursor is persisted
    await publish_mission(bus, EventType.MISSION_STARTED, mission())  # re-sent after a restart
    await h.catch_up(bus)
    (row,) = await list_missions(store)
    assert row.outcome is MissionOutcome.CANCELLED


async def test_a_new_start_closes_a_stale_open_mission(store, bus) -> None:
    h = History(store)
    await publish_mission(bus, EventType.MISSION_STARTED, mission("OLD"))
    await publish_mission(bus, EventType.MISSION_STARTED, mission("NEW"))
    await publish_mission(bus, EventType.MISSION_STARTED, mission("OTHER", robot=M6))
    await h.catch_up(bus)
    outcomes = {(m.robot_id, m.id): m.outcome for m in await list_missions(store)}
    assert outcomes == {
        (I5, "OLD"): MissionOutcome.UNKNOWN,
        (I5, "NEW"): MissionOutcome.IN_PROGRESS,
        (M6, "OTHER"): MissionOutcome.IN_PROGRESS,
    }
    restorable = await h.restorable()
    assert [(m.id, m.outcome.value) for m in restorable if m.robot_id == I5] == [
        ("OLD", "unknown"),
        ("NEW", "in_progress"),
    ]


async def test_filters_and_newest_first(store, bus) -> None:
    h = History(store)
    for n, outcome in enumerate(["completed", "cancelled", "completed"]):
        m = mission(f"M{n}", outcome=outcome)
        m.started_at = T0 + timedelta(hours=n)
        await publish_mission(bus, EventType.MISSION_FINISHED, m)
    await h.catch_up(bus)
    assert [m.id for m in await list_missions(store)] == ["M2", "M1", "M0"]
    assert [m.id for m in await list_missions(store, outcome=MissionOutcome.COMPLETED)] == [
        "M2",
        "M0",
    ]
    window = await list_missions(
        store, since=T0 + timedelta(hours=1), until=T0 + timedelta(hours=2)
    )
    assert [m.id for m in window] == ["M1"]
    assert [m.id for m in await list_missions(store, limit=1)] == ["M2"]
    assert await list_missions(store, robot_id=M6) == []


def test_cloud_ref_by_id_then_by_start_time() -> None:
    start = int(T0.timestamp())
    records = [{"missionId": "C1", "startTime": start + 30}, {"missionId": "M1", "startTime": 0}]
    assert cloud_ref(mission("M1"), records) == "M1"
    assert cloud_ref(mission("kit-uuid"), records) == "C1"
    assert cloud_ref(mission("kit-uuid"), [{"missionId": "C1", "startTime": start + 600}]) is None


async def test_link_cloud_sets_the_ref_and_tolerates_failures(store, bus) -> None:
    h = History(store)
    ended = T0 + timedelta(minutes=30)
    await publish_mission(
        bus, EventType.MISSION_FINISHED, mission(outcome="completed", ended_at=ended)
    )
    await publish_mission(bus, EventType.MISSION_STARTED, mission("LIVE", robot=M6))
    await h.catch_up(bus)
    cloud = FakeCloud(history={I5: [{"missionId": "M1", "startTime": int(T0.timestamp())}]})
    cloud.fail_everything = True
    assert await h.link_cloud(cloud, now=ended) == 0
    cloud.fail_everything = False
    assert await h.link_cloud(cloud, now=ended) == 1
    assert await h.link_cloud(cloud, now=ended) == 0  # already linked; open missions never
    assert cloud.calls.count("mission_history") == 2
    assert (await list_missions(store, robot_id=I5))[0].cloud_mission_ref == "M1"
    # Missions finished more than a day ago aren't looked up any more.
    assert await h.link_cloud(FakeCloud(), now=ended + timedelta(days=2)) == 0


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[EventType, dict]] = []

    async def publish(self, type_, robot_id, data, at=None) -> None:
        self.events.append((type_, encode_data(type_, data)))

    def types(self) -> list[EventType]:
        return [t for t, _ in self.events]


async def feed(d: Deriver, at: datetime, **status) -> None:
    reported = merge({"batPct": 90}, {"cleanMissionStatus": status})
    await d.update(normalize(I5, reported, I5_PROFILE, at))


async def test_restore_continues_the_same_mission_without_a_new_start() -> None:
    rec = Recorder()
    d = Deriver(rec, new_id=lambda: "kit-0")  # type: ignore[arg-type]
    d.restore([mission("M0", outcome="completed"), mission("M1", errors=[JAM])])
    assert not d.seen(I5)
    await feed(d, T0 + timedelta(minutes=5), phase="run", cycle="clean", missionId="M1")
    assert EventType.MISSION_STARTED not in rec.types()
    current = d.current(I5)
    assert current is not None and current.id == "M1" and current.started_at == T0
    await feed(d, T0 + timedelta(minutes=9), phase="charge", cycle="none", missionId="M1")
    (finished,) = [data for t, data in rec.events if t is EventType.MISSION_FINISHED]
    assert finished["mission"]["id"] == "M1" and finished["mission"]["errors"] == [JAM.model_dump()]
    assert finished["mission"]["duration_s"] == 540


@pytest.mark.parametrize(
    ("status", "types"),
    [
        ({"phase": "charge", "cycle": "none", "missionId": "M1"}, [EventType.MISSION_FINISHED]),
        (
            {"phase": "run", "cycle": "clean", "missionId": "M2"},
            [EventType.MISSION_FINISHED, EventType.MISSION_STARTED],
        ),
    ],
    ids=["ended-while-down", "moved-on"],
)
async def test_restore_closes_a_mission_the_robot_left(status, types) -> None:
    rec = Recorder()
    d = Deriver(rec, new_id=lambda: "kit-0")  # type: ignore[arg-type]
    d.restore([mission("M1")])
    await feed(d, T0 + timedelta(hours=1), **status)
    missions = [(t, data["mission"]) for t, data in rec.events if "mission" in data]
    assert [t for t, _ in missions] == types
    assert missions[0][1]["id"] == "M1" and missions[0][1]["outcome"] == "unknown"
    assert missions[0][1]["ended_at"] is None


async def test_restored_finished_ids_count_as_reused() -> None:
    rec = Recorder()
    d = Deriver(rec, new_id=lambda: "kit-0")  # type: ignore[arg-type]
    d.restore([mission("M1", outcome="completed")])
    await feed(d, T0 + timedelta(hours=1), phase="run", cycle="clean", missionId="M1")
    current = d.current(I5)
    assert current is not None and current.id == "kit-0"
