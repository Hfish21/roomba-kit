"""Settings operations: read every supported setting, and change one with read-back."""

from pydantic import BaseModel, Field

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import SettingChange
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service import settings
from roomba_kit.service.app import ServiceContext

SettingValue = bool | int | str | None


class RobotRef(BaseModel):
    robot: str = Field(description="Robot ID or name")


class SetSettingParams(BaseModel):
    robot: str = Field(description="Robot ID or name")
    setting: str = Field(
        description="Setting name: passes, edge_clean, always_finish, child_lock, eco_charge, "
        "schedule_hold, pad_wetness, spray_overlap or carpet_boost"
    )
    value: bool | int | str = Field(
        description="passes: auto|one|two. edge_clean, always_finish, child_lock, eco_charge, "
        "schedule_hold, carpet_boost: true|false. pad_wetness: 1|2|3. "
        "spray_overlap: standard|deep|extended."
    )


@operation(
    description="Read the robot's settings: every setting its model supports, null when the "
    "robot hasn't reported it.",
    params=RobotRef,
    result=dict[str, SettingValue],
    read_only=True,
    http=Http("GET", "/robots/{robot}/settings"),
    cli=Cli(("settings",), "show", args=("robot",)),
)
async def get_settings(ctx: ServiceContext, params: RobotRef) -> dict[str, SettingValue]:
    return settings.read_settings(ctx.resolve_robot(params.robot))


@operation(
    description="Change one setting. It is reported applied only once the robot reports the "
    "new value; otherwise the error is `not_applied` with the value the robot holds.",
    params=SetSettingParams,
    result=SettingChange,
    http=Http("PUT", "/robots/{robot}/settings/{setting}"),
    cli=Cli(("settings",), "set", args=("robot", "setting", "value")),
)
async def set_setting(ctx: ServiceContext, params: SetSettingParams) -> SettingChange:
    change = await settings.change_setting(
        ctx.resolve_robot(params.robot), params.setting, params.value
    )
    if change.outcome == "not_applied":
        # errors.md: `not_applied` is 409 / exit 3 with `details.actual`. The details carry the
        # whole SettingChange, so callers see the same fields either way.
        raise KitError(
            ErrorCode.NOT_APPLIED,
            f"The robot accepted {params.setting}={change.requested} but reports {change.actual}.",
            change.robot_id,
            change.model_dump(mode="json"),
        )
    return change
