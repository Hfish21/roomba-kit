"""Local mission history queries (T087, FR-044, FR-045)."""

from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import Mission, MissionDetail, MissionOutcome
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service import history
from roomba_kit.service.app import ServiceContext


class ListMissionsParams(BaseModel):
    robot: str | None = Field(
        None,
        description="Only this robot's missions (ID or unambiguous name). A removed robot's "
        "missions are still found by its ID.",
    )
    since: datetime | None = Field(
        None, description="Only missions started at or after this time (ISO 8601; UTC if no offset)"
    )
    until: datetime | None = Field(
        None, description="Only missions started before this time (ISO 8601; UTC if no offset)"
    )
    outcome: MissionOutcome | None = Field(None, description="Only missions with this outcome")
    limit: int = Field(50, ge=1, le=1000, description="Most missions to return")

    @field_validator("since", "until")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value and value.tzinfo is None else value


class GetMissionParams(BaseModel):
    mission_id: str = Field(description="Mission ID, as listed by `list_missions`")


async def _robot_id(ctx: ServiceContext, ref: str | None) -> str | None:
    if ref is None:
        return None
    try:
        return ctx.resolve_robot(ref).robot_id
    except KitError as err:
        if err.code is ErrorCode.ROBOT_NOT_FOUND and await history.has_robot(ctx.store, ref):
            return ref
        raise


@operation(
    description=(
        "List the missions recorded in local history, newest first: robot, start and end, "
        "outcome, rooms requested (empty means the whole home), who started it, duration and "
        "errors. History is kept indefinitely and includes the mission in progress."
    ),
    params=ListMissionsParams,
    result=list[Mission],
    read_only=True,
    http=Http("GET", "/missions"),
    cli=Cli(("missions",), "list"),
)
async def list_missions(ctx: ServiceContext, params: ListMissionsParams) -> list[Mission]:
    return await history.list_missions(
        ctx.store,
        robot_id=await _robot_id(ctx, params.robot),
        since=params.since,
        until=params.until,
        outcome=params.outcome,
        limit=params.limit,
    )


@operation(
    description="Get one mission from local history by its ID, with its activity transitions "
    "(cleaning, paused, returning, docked, ...) oldest first.",
    params=GetMissionParams,
    result=MissionDetail,
    read_only=True,
    http=Http("GET", "/missions/{mission_id}"),
    cli=Cli(("missions",), "show", args=("mission_id",)),
)
async def get_mission(ctx: ServiceContext, params: GetMissionParams) -> MissionDetail:
    mission = await history.get_mission(ctx.store, params.mission_id)
    if mission is None:
        raise KitError(
            ErrorCode.INVALID_REQUEST,
            f"No mission {params.mission_id!r} in local history.",
            details={"mission_id": params.mission_id},
        )
    return mission
