"""Cloud records: floor plan, cloud mission history and per-mission coverage (FR-041–FR-043).

Optional extras (Constitution I). Each returns `cloud_unavailable` with `details.reason` when the
cloud can't answer; floor plans and history fall back to the last cached copy, with its
`retrieved_at`. The live timeline is delivered as `cloud.timeline` events, not an operation.
"""

from pydantic import BaseModel, Field

from roomba_kit.core.capabilities import Capability
from roomba_kit.core.models import CoverageRecord
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service import cloud_data
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.cloud_data import CloudMission, FloorPlan


class RobotParams(BaseModel):
    robot: str = Field(description="Robot ID or name")


class CloudHistoryParams(RobotParams):
    limit: int = Field(30, ge=1, le=100, description="Most recent missions to return")


class CoverageParams(RobotParams):
    mission_id: str = Field(
        description="The kit mission ID (the robot's own mission ID) or the cloud mission ref"
    )


@operation(
    description=(
        "The robot's stored map from the iRobot cloud: its rooms with names, types and outlines "
        "(rings of [x, y] metres, outer ring first). While the cloud is unavailable, the last "
        "copy fetched is returned with its `retrieved_at`."
    ),
    params=RobotParams,
    result=FloorPlan,
    capability=Capability.CLOUD_ROOMS,
    read_only=True,
    http=Http("GET", "/robots/{robot}/floor-plan"),
    cli=Cli((), "floor-plan", args=("robot",)),
)
async def get_floor_plan(ctx: ServiceContext, params: RobotParams) -> FloorPlan:
    session = ctx.resolve_robot(params.robot)
    session.require(Capability.CLOUD_ROOMS)
    return await cloud_data.floor_plan(ctx, session.robot_id)


@operation(
    description=(
        "The robot's recent missions as the iRobot cloud recorded them (it keeps about 30), "
        "newest first. A running mission appears only minutes after the robot docks. Always "
        "fetched live: while the cloud is unavailable this returns `cloud_unavailable` with a "
        "reason."
    ),
    params=CloudHistoryParams,
    result=list[CloudMission],
    capability=Capability.CLOUD_HISTORY,
    read_only=True,
    http=Http("GET", "/robots/{robot}/cloud-history"),
    cli=Cli(("cloud",), "history", args=("robot",)),
)
async def cloud_history(ctx: ServiceContext, params: CloudHistoryParams) -> list[CloudMission]:
    session = ctx.resolve_robot(params.robot)
    session.require(Capability.CLOUD_HISTORY)
    return await cloud_data.cloud_history(ctx, session.robot_id, params.limit)


@operation(
    description=(
        "Where a finished mission cleaned: covered points, dirt detections and clutter, in "
        "metres from the dock. The cloud publishes it minutes after the robot docks; until then "
        "this returns `cloud_unavailable` with reason `not_yet_published`. Clutter is given as "
        "the vertices of each clutter area's outline."
    ),
    params=CoverageParams,
    result=CoverageRecord,
    capability=Capability.CLOUD_COVERAGE,
    read_only=True,
    http=Http("GET", "/robots/{robot}/missions/{mission_id}/coverage"),
    cli=Cli((), "coverage", args=("robot", "mission_id")),
)
async def get_coverage(ctx: ServiceContext, params: CoverageParams) -> CoverageRecord:
    session = ctx.resolve_robot(params.robot)
    session.require(Capability.CLOUD_COVERAGE)
    return await cloud_data.coverage(ctx, session.robot_id, params.mission_id)
