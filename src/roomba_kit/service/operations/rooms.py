"""Rooms and room cleaning (US3: FR-008, FR-031, FR-032, FR-043).

The CLI shows `clean_rooms` as `roomba-kit clean ROBOT --room …` (cli/aliases.py); the
generated command stays reachable at `roomba-kit rooms clean`.
"""

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from roomba_kit.core.capabilities import Capability
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import CommandResult, KitModel, MapRef, Room
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service import rooms
from roomba_kit.service.app import ServiceContext


class RobotParams(BaseModel):
    robot: str = Field(description="Robot ID or unambiguous name")


class RoomList(KitModel):
    map: MapRef | None = Field(
        description="The robot's current map (the newest stored one while it hasn't reported "
        "state); null when nothing is known about its map."
    )
    rooms: list[Room] = Field(
        description="Rooms on that map: imported, locally named and learned. Rooms stored for "
        "an older map version are included with their own `map_version`."
    )
    stale: bool = Field(
        description="True when some rooms are for an older map: using them fails with "
        "`map_changed` until rooms are re-imported or named again."
    )
    retrieved_at: datetime | None = Field(
        default=None, description="When this map's rooms were last imported from the cloud."
    )


@operation(
    description="List the robot's rooms: imported from the cloud, named locally, or learned from "
    "room cleans the robot reported (including ones started by other apps). Works without the "
    "cloud. Unnamed rooms are shown as `Room <id>`.",
    params=RobotParams,
    result=RoomList,
    read_only=True,
    http=Http("GET", "/robots/{robot}/rooms"),
    cli=Cli(("rooms",), "list", args=("robot",)),
)
async def list_rooms(ctx: ServiceContext, params: RobotParams) -> RoomList:
    session = ctx.resolve_robot(params.robot)
    found = await rooms.room_set(ctx.store, session)
    known = {r.room_id for r in found.usable}
    listed = [*found.usable, *(r for r in found.stale if r.room_id not in known)]
    return RoomList(
        map=found.map,
        rooms=sorted(listed, key=rooms.sort_key),
        stale=bool(found.stale),
        retrieved_at=found.retrieved_at,
    )


class ImportResult(KitModel):
    imported: int = Field(description="Rooms on the imported map")
    map: MapRef


@operation(
    description="Import the robot's room IDs and names from its stored map in the iRobot cloud. "
    "Replaces rooms stored for older map versions; local names on the same map version are "
    "kept. The result is cached, so rooms stay usable while the cloud is down.",
    params=RobotParams,
    result=ImportResult,
    capability=Capability.CLOUD_ROOMS,
    http=Http("POST", "/robots/{robot}/rooms/import"),
    cli=Cli(("rooms",), "import", args=("robot",)),
)
async def import_rooms(ctx: ServiceContext, params: RobotParams) -> ImportResult:
    session = ctx.resolve_robot(params.robot)
    session.require(Capability.CLOUD_ROOMS)
    count, ref = await rooms.import_rooms(ctx, session)
    return ImportResult(imported=count, map=ref)


class NameRoomParams(BaseModel):
    robot: str = Field(description="Robot ID or unambiguous name")
    room_id: str = Field(description="The room's ID on the robot's current map")
    name: str = Field(
        min_length=1,
        description="The local name. Unique per map, ignoring case; it overrides the cloud name.",
    )


@operation(
    description="Give a room on the robot's current map a local name. Works without the "
    "cloud, for imported and learned rooms alike. Names apply to one map version.",
    params=NameRoomParams,
    result=Room,
    http=Http("PUT", "/robots/{robot}/rooms/{room_id}"),
    cli=Cli(("rooms",), "name", args=("robot", "room_id", "name")),
)
async def name_room(ctx: ServiceContext, params: NameRoomParams) -> Room:
    session = ctx.resolve_robot(params.robot)
    return await rooms.name_room(ctx.store, session, params.room_id, params.name)


class RoomOptions(BaseModel):
    passes: rooms.Passes | None = Field(
        default=None, description="Cleaning passes in this room: auto, one or two (vacuums)."
    )


class CleanRoomsParams(BaseModel):
    robot: str = Field(description="Robot ID or unambiguous name")
    rooms: list[str] = Field(
        description="Rooms to clean, by name or ID, in the order to clean them. Must not be "
        "empty (the robot would clean the whole home)."
    )
    ordered: bool = Field(
        default=True, description="Clean in the given order; false lets the robot choose."
    )
    room_options: dict[str, RoomOptions] | None = Field(
        default=None,
        description='Per-room options keyed by an entry of `rooms`, e.g. {"Office": {"passes": '
        '"two"}}. On the CLI, pass it as JSON.',
    )

    @field_validator("rooms")
    @classmethod
    def _not_empty(cls, value: list[str]) -> list[str]:
        if not value or any(not ref.strip() for ref in value):
            raise ValueError(
                "give at least one room; an empty room list would clean the whole home"
            )
        return value

    @field_validator("room_options", mode="before")
    @classmethod
    def _from_json(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError:
                raise ValueError("room_options must be a JSON object") from None
        return value


@operation(
    description="Clean only the given rooms, in order, by name or ID. Refused with "
    "`map_changed` when the names come from an older map version, and with `room_not_found` "
    "(listing the valid rooms) when a room isn't on the current map. Confirmed when the robot "
    "reports cleaning.",
    params=CleanRoomsParams,
    result=CommandResult,
    capability=Capability.CLEAN_ROOMS,
    motion=True,
    http=Http("POST", "/robots/{robot}/commands/clean_rooms"),
    cli=Cli(("rooms",), "clean", args=("robot",)),
)
async def clean_rooms(ctx: ServiceContext, params: CleanRoomsParams) -> CommandResult:
    session = ctx.resolve_robot(params.robot)
    session.require(Capability.CLEAN_ROOMS)
    options = params.room_options or {}
    if unknown := set(options) - set(params.rooms):
        raise KitError(
            ErrorCode.INVALID_REQUEST,
            "room_options keys must be entries of rooms.",
            session.robot_id,
            {"unknown": sorted(unknown)},
        )
    if any(o.passes for o in options.values()):
        session.require(Capability.SETTING_PASSES)
    targets = await rooms.resolve(ctx.store, session, params.rooms)
    by_ref = dict(zip(params.rooms, targets, strict=True))
    passes = {by_ref[ref].room_id: opt.passes for ref, opt in options.items() if opt.passes}
    protocol = await rooms.clean_params(
        ctx.store, session, targets, ordered=params.ordered, passes=passes
    )
    return await session.execute("clean_rooms", protocol)
