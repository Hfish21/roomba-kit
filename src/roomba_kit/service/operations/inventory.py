"""Robot inventory: the configured robots, their models and capabilities.

A robot whose model has no profile is still listed, with `supported=false` and no
capabilities; every command on it fails with `unsupported_model` before contacting it.
"""

from pydantic import BaseModel, Field

from roomba_kit.core.capabilities import CapabilityStatus
from roomba_kit.core.models import Robot
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service.app import ServiceContext


class RobotRef(BaseModel):
    robot: str = Field(description="Robot ID or unambiguous name")


@operation(
    description="List every configured robot: ID, name, model, SKU, firmware, address, "
    "connection state and capabilities with their verification status. Robots without a "
    "model profile are listed with `supported=false` (see docs/guides/adding-a-model.md).",
    result=list[Robot],
    read_only=True,
    http=Http("GET", "/robots"),
    cli=Cli(("robots",), "list"),
)
async def list_robots(ctx: ServiceContext, params: BaseModel) -> list[Robot]:
    return [session.robot() for session in ctx]


@operation(
    description="One robot's details: ID, name, model, SKU, firmware, address, connection "
    "state and capabilities.",
    params=RobotRef,
    result=Robot,
    read_only=True,
    http=Http("GET", "/robots/{robot}"),
    cli=Cli(("robots",), "show", args=("robot",)),
)
async def get_robot(ctx: ServiceContext, params: RobotRef) -> Robot:
    return ctx.resolve_robot(params.robot).robot()


@operation(
    description="What the robot's model supports, each with `verified` (exercised on real "
    "hardware) or `unverified`. Empty for a model without a profile.",
    params=RobotRef,
    result=list[CapabilityStatus],
    read_only=True,
    http=Http("GET", "/robots/{robot}/capabilities"),
    cli=Cli((), "capabilities", args=("robot",)),
)
async def get_capabilities(ctx: ServiceContext, params: RobotRef) -> list[CapabilityStatus]:
    return ctx.resolve_robot(params.robot).robot().capabilities
