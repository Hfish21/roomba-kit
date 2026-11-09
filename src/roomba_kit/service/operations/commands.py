"""Robot commands (FR-028–FR-030). Each one runs through the robot's FIFO queue and returns a
`CommandResult` once the state change is confirmed, or `unconfirmed` when the window expires.
"""

from pydantic import BaseModel, Field

from roomba_kit.core.capabilities import Capability
from roomba_kit.core.models import CommandResult
from roomba_kit.core.registry import Cli, Handler, Http, operation
from roomba_kit.service.app import ServiceContext


class CommandParams(BaseModel):
    robot: str = Field(description="Robot ID or unambiguous name")


COMMANDS: dict[str, str] = {
    "clean": "Start cleaning the whole home. Confirmed when the robot reports cleaning.",
    "pause": "Pause the current mission. Confirmed when the robot reports paused.",
    "resume": "Resume a paused mission. Confirmed when the robot reports cleaning again.",
    "stop": "End the current mission where the robot is. Confirmed when no mission is active.",
    "dock": "Send the robot back to its dock. Confirmed when it reports returning, docked or "
    "charging.",
    "find": "Play the locate sound so you can find the robot. It doesn't move. Confirmed when the "
    "robot echoes the command.",
    "evac": "Empty the bin into the Clean Base. Confirmed when the robot reports emptying.",
    "reboot": "Soft-reboot the robot. Confirmed when its connection drops and comes back (up to "
    "3 minutes).",
    "train": "Start a mapping run with no cleaning. Confirmed when the robot reports mapping. "
    "⚠ This can re-create the stored map and "
    "change room IDs, which breaks room names and room-clean requests that use the old IDs.",
}


def _handler(command: str) -> Handler:
    async def run(ctx: ServiceContext, params: CommandParams) -> CommandResult:
        return await ctx.resolve_robot(params.robot).execute(command)

    run.__name__ = command
    return run


for _command, _description in COMMANDS.items():
    operation(
        name=_command,
        description=_description,
        params=CommandParams,
        result=CommandResult,
        capability=Capability(_command),
        motion=_command != "find",
        http=Http("POST", f"/robots/{{robot}}/commands/{_command}"),
        cli=Cli((), _command, args=("robot",)),
    )(_handler(_command))
