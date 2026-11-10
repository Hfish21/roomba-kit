"""Room storage, naming, resolution and room-clean params (service/rooms.py)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import CommandEcho, Initiator, MapRef, RobotState, Room
from roomba_kit.profiles.roomba_i5 import PROFILE
from roomba_kit.service import rooms
from roomba_kit.service.operations.rooms import CleanRoomsParams
from roomba_kit.service.store import Store

I5 = "00000000000000000000000000000001"
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


@dataclass
class Session:
    """The slice of RobotSession that service/rooms.py reads."""

    state: RobotState = field(
        default_factory=lambda: RobotState(
            robot_id=I5, at=NOW, map=MapRef(map_id="m", version="v2")
        )
    )
    has_state: bool = True
    robot_id: str = I5
    profile: object = PROFILE

    def require_link(self) -> None:
        raise KitError(ErrorCode.ROBOT_OFFLINE, "offline", I5)


@pytest.fixture
async def store(tmp_path):
    async with Store(tmp_path / "state.db") as s:
        yield s


async def add(store, room_id, *, version="v2", cloud=None, local=None):
    await store.execute(
        "INSERT INTO rooms (robot_id, map_id, map_version, room_id, cloud_name, local_name)"
        " VALUES (?, 'm', ?, ?, ?, ?)",
        (I5, version, room_id, cloud, local),
    )


def room(room_id, cloud=None, local=None):
    return Room(
        robot_id=I5,
        map_id="m",
        map_version="v2",
        room_id=room_id,
        cloud_name=cloud,
        local_name=local,
    )


def test_effective_name_prefers_local_then_cloud_then_placeholder():
    assert room("1", "Kitchen", "Galley").name == "Galley"
    assert room("1", "Kitchen").name == "Kitchen"
    assert room("1").name == "Room 1"


def test_match_by_id_then_name_ignoring_case():
    found = [room("1", "Kitchen"), room("2", "1")]
    assert rooms.match(found, "1").room_id == "1"
    assert rooms.match(found, "  KITCHEN ").room_id == "1"
    assert rooms.match(found, "Hall") is None


def test_a_shared_cloud_name_is_suffixed_and_ambiguous():
    names = rooms._unique_names(
        [("4", "Office", None), ("5", "office", None), ("6", "Den", None), ("7", None, None)],
        local={"6": "Room 7"},
    )
    assert [n for _, n, _ in names] == ["Office (4)", "office (5)", "Den", "Room 7 (7)"]
    stored = [room(i, n) for i, n, _ in names]
    with pytest.raises(KitError) as exc:
        rooms.match(stored, "Office")
    assert exc.value.code is ErrorCode.AMBIGUOUS_NAME
    assert len(exc.value.details["candidates"]) == 2
    assert rooms.match(stored, "office (5)").room_id == "5"


async def test_learned_rooms_join_their_map_version_and_old_versions_are_stale(store):
    await add(store, "1", cloud="Kitchen")
    await add(store, "3", version="v1", local="Den")
    await rooms.record_learned(store, I5, "m", ["3", "1"], NOW, "v2")
    await rooms.record_learned(store, I5, "m", ["8"], NOW)  # version unknown: history only
    found = await rooms.room_set(store, Session())
    assert [(r.room_id, r.name) for r in found.usable] == [("1", "Kitchen"), ("3", "Room 3")]
    assert [r.name for r in found.stale] == ["Den"]
    with pytest.raises(KitError) as exc:
        await rooms.resolve(store, Session(), ["Den"])
    assert exc.value.code is ErrorCode.MAP_CHANGED
    assert exc.value.details["requested"] == {"map_id": "m", "version": "v1"}


async def test_without_state_the_newest_stored_map_is_shown(store):
    await add(store, "1", version="v1", cloud="Old")
    await add(store, "1", version="v3", cloud="New")
    found = await rooms.room_set(store, Session(has_state=False))
    assert found.map == MapRef(map_id="m", version="v3")
    assert [r.name for r in found.usable] == ["New"]
    with pytest.raises(KitError) as exc:
        await rooms.resolve(store, Session(has_state=False), ["New"])
    assert exc.value.code is ErrorCode.ROBOT_OFFLINE


async def test_no_map_is_refused(store):
    session = Session(state=RobotState(robot_id=I5, at=NOW))
    with pytest.raises(KitError) as exc:
        await rooms.resolve(store, session, ["1"])
    assert exc.value.code is ErrorCode.NO_MAP


async def test_learned_rooms_come_only_from_new_room_clean_echoes(store):
    learned = rooms.LearnedRooms(store)
    base = Session().state
    clean = base.model_copy(
        update={
            "last_command": CommandEcho(
                command="clean_rooms", initiator=Initiator.APP, room_ids=["7", "3"], map_id="m"
            )
        }
    )
    await learned.on_state(Session(), base, clean)
    find = clean.model_copy(
        update={"last_command": clean.last_command.model_copy(update={"command": "find"})}
    )
    await learned.on_state(Session(), clean, find)  # stale room fields merged under a find
    other_map = clean.model_copy(
        update={
            "last_command": clean.last_command.model_copy(
                update={"map_id": None, "room_ids": ["9"]}
            )
        }
    )
    await learned.on_state(Session(), clean, other_map)  # falls back to the robot's map
    rows = await store.fetchall("SELECT map_id, room_id FROM learned_rooms ORDER BY room_id")
    assert [tuple(r) for r in rows] == [("m", "3"), ("m", "7"), ("m", "9")]
    assert {r.room_id for r in (await rooms.room_set(store, Session())).usable} == {"3", "7", "9"}


async def test_an_echo_replayed_at_startup_counts_only_if_it_names_the_current_version(store):
    learned = rooms.LearnedRooms(store)
    state = Session().state
    for room_id, version in (("4", "v1"), ("5", "v2")):
        echo = CommandEcho(
            command="clean_rooms",
            initiator=Initiator.APP,
            room_ids=[room_id],
            map_id="m",
            map_version=version,
        )
        await learned.on_state(Session(), None, state.model_copy(update={"last_command": echo}))
    usable = (await rooms.room_set(store, Session())).usable
    assert [r.room_id for r in usable] == ["5"]
    assert len(await store.fetchall("SELECT * FROM learned_rooms")) == 2


async def test_naming_rejects_clashes_and_id_like_names(store):
    await add(store, "1", cloud="Kitchen")
    await rooms.record_learned(store, I5, "m", ["3"], NOW, "v2")
    named = await rooms.name_room(store, Session(), "3", " Den ")
    assert (named.local_name, named.name) == ("Den", "Den")
    renamed = await rooms.name_room(store, Session(), "1", "Galley")
    assert renamed.cloud_name == "Kitchen" and renamed.name == "Galley"
    for clash in ("den", "7", ""):
        with pytest.raises(KitError) as exc:
            await rooms.name_room(store, Session(), "1", clash)
        assert exc.value.code is ErrorCode.INVALID_REQUEST
    with pytest.raises(KitError) as exc:
        await rooms.name_room(store, Session(), "9", "Hall")
    assert exc.value.code is ErrorCode.ROOM_NOT_FOUND
    assert exc.value.details["valid"] == [{"id": "1", "name": "Galley"}, {"id": "3", "name": "Den"}]


async def test_clean_params_dedupe_and_pick_the_user_map_version(store):
    session = Session()
    params = await rooms.clean_params(
        store, session, [room("7"), room("4"), room("7")], ordered=False, passes={"4": "two"}
    )
    assert params == {
        "pmap_id": "m",
        "user_pmapv_id": "v2",
        "ordered": 0,
        "regions": [
            {"region_id": "7", "type": "rid"},
            {"region_id": "4", "type": "rid", "params": {"noAutoPasses": True, "twoPass": True}},
        ],
    }
    session.state.last_command = CommandEcho(
        command="clean", initiator=Initiator.APP, map_id="m", map_version="u1"
    )
    assert (await rooms.clean_params(store, session, [room("7")], ordered=True))[
        "user_pmapv_id"
    ] == "u1"
    await store.execute(
        "INSERT INTO cloud_cache VALUES (?, ?, 'm', ?, ?)",
        (
            I5,
            rooms.CACHE_KIND,
            '{"active_pmapv_id": "v2", "user_pmapv_id": "u0"}',
            "2026-11-08T12:00:00.000000Z",
        ),
    )
    assert (await rooms.clean_params(store, session, [room("7")], ordered=True))[
        "user_pmapv_id"
    ] == "u0"
    with pytest.raises(KitError):
        await rooms.clean_params(store, session, [], ordered=True)


def test_clean_rooms_params_refuse_an_empty_list_and_parse_options_json():
    with pytest.raises(ValueError, match="whole home"):
        CleanRoomsParams(robot="r", rooms=[])
    params = CleanRoomsParams(
        robot="r", rooms=["Office"], room_options='{"Office": {"passes": "one"}}'
    )
    assert params.room_options["Office"].passes == "one"
