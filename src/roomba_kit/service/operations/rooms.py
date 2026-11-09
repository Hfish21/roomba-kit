"""Rooms: listing them and importing them from the cloud (US3: FR-008, FR-032, FR-043)."""

from datetime import datetime

from pydantic import BaseModel, Field

from roomba_kit.core.capabilities import Capability
from roomba_kit.core.models import KitModel, MapRef, Room
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
