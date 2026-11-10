"""Live state on demand (T049, FR-026, FR-027)."""

from pydantic import BaseModel, Field

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import ADD_A_MODEL_GUIDE, RobotState
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service.app import ServiceContext


class RobotParams(BaseModel):
    robot: str = Field(description="Robot ID or unambiguous name")


@operation(
    description=(
        "Get a robot's full live state: battery, activity, current mission, last command, "
        "error and not-ready codes with readable text, Wi-Fi signal, stored map, robot "
        "schedules, lifetime counters, settings, and the bin, Clean Base, pad, tank and lid "
        "where the model has them. `at` is when the robot last reported; while it's offline "
        "this is the last state it reported."
    ),
    params=RobotParams,
    result=RobotState,
    read_only=True,
    http=Http("GET", "/robots/{robot}/state"),
    cli=Cli((), "state", args=("robot",)),
)
async def get_state(ctx: ServiceContext, params: RobotParams) -> RobotState:
    session = ctx.resolve_robot(params.robot)
    if not session.supported:
        raise KitError(
            ErrorCode.UNSUPPORTED_MODEL,
            "This robot's model has no profile, so its state can't be read. To add one, see "
            f"{ADD_A_MODEL_GUIDE}.",
            session.robot_id,
        )
    if not session.has_state:
        session.require_link()  # raises why there's no link
        raise KitError(
            ErrorCode.ROBOT_OFFLINE,
            "The robot is connected but hasn't reported its state yet.",
            session.robot_id,
        )
    return session.state
