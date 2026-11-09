"""Live position on request (FR-039). The stream is `position` events (service/position.py)."""

from pydantic import BaseModel, Field

from roomba_kit.core.capabilities import Capability
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import NoFix, PositionFix
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service.app import ServiceContext

REQUEST_TIMEOUT_S = 2.0


class PositionParams(BaseModel):
    robot: str = Field(description="Robot ID or name")


@operation(
    description=(
        "Where the robot is now: x/y in metres from the dock and heading in radians, with the "
        "map it's on. `{no_fix: true}` when it has no fix (for example while docked); that is "
        "not an error. During missions the same fixes stream as `position` events."
    ),
    params=PositionParams,
    result=PositionFix | NoFix,
    capability=Capability.LIVE_POSITION,
    read_only=True,
    http=Http("GET", "/robots/{robot}/position"),
    cli=Cli((), "position", args=("robot",)),
)
async def get_position(ctx: ServiceContext, params: PositionParams) -> PositionFix | NoFix:
    session = ctx.resolve_robot(params.robot)
    session.require(Capability.LIVE_POSITION)  # before any robot contact
    try:
        fix = await session.request_position(REQUEST_TIMEOUT_S)
    except TimeoutError:
        raise KitError(
            ErrorCode.TIMEOUT,
            f"The robot didn't answer a position request within {REQUEST_TIMEOUT_S:g} s.",
            session.robot_id,
        ) from None
    return fix if fix is not None else NoFix()
