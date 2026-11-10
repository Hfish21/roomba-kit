"""iRobot account operations: log in, log out, status, and password retrieval through the cloud.

The account is optional. `cloud_login` takes a password, so it's on HTTP and
the CLI but not MCP (plan Complexity Tracking).
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import ConnectionState, KitModel
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service.app import CloudStatus, ServiceContext
from roomba_kit.service.cloud import manager
from roomba_kit.service.operations.setup import store_verified_password

_CLOUD_ERRORS = (ErrorCode.AUTH_FAILED, ErrorCode.CLOUD_UNAVAILABLE)


def _reason(err: KitError) -> str:
    if err.code is ErrorCode.AUTH_FAILED:
        return ErrorCode.AUTH_FAILED.value
    return str((err.details or {}).get("reason") or err.code.value)


class CloudLogin(BaseModel):
    username: str = Field(min_length=1, description="iRobot account email")
    password: str = Field(
        min_length=1,
        description="iRobot account password. The CLI prompts for it; it is never echoed, "
        "returned or logged.",
    )
    country: str = Field(
        "US", pattern=r"^[A-Za-z]{2}$", description="The account's country code, e.g. US, GB, DE"
    )

    @field_validator("country")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class CloudLoginResult(KitModel):
    status: Literal["connected", "failed"]
    reason: str | None = Field(
        default=None, description="Why it failed: `auth_failed`, `timeout`, `connection_failed`, …"
    )


@operation(
    description="Add or change the iRobot account login. It's checked by logging in and saved "
    "only if that works. Optional: it adds room names, floor plans, cloud history, coverage, the "
    "live mission timeline and password retrieval; local control never needs it.",
    params=CloudLogin,
    result=CloudLoginResult,
    http=Http("POST", "/cloud_login"),
    cli=Cli(("cloud",), "login", secret=("password",)),
    surfaces={"http", "cli"},
)
async def cloud_login(ctx: ServiceContext, params: CloudLogin) -> CloudLoginResult:
    try:
        await manager(ctx).configure(params.username, params.password, params.country)
    except KitError as err:
        if err.code in _CLOUD_ERRORS:
            return CloudLoginResult(status="failed", reason=_reason(err))
        raise
    return CloudLoginResult(status="connected")


@operation(
    description="Remove the stored iRobot account login and disconnect from the cloud. "
    "Credentials supplied by environment variables still apply.",
    result=CloudStatus,
    http=Http("POST", "/cloud_logout"),
    cli=Cli(("cloud",), "logout"),
)
async def cloud_logout(ctx: ServiceContext, params: BaseModel) -> CloudStatus:
    return await manager(ctx).clear()


@operation(
    description="The cloud subsystem's status: `connected`, `unavailable` (with a reason) or "
    "`not_configured`, and when it last worked.",
    result=CloudStatus,
    read_only=True,
    http=Http("GET", "/cloud_status"),
    cli=Cli(("cloud",), "status"),
)
async def cloud_status(ctx: ServiceContext, params: BaseModel) -> CloudStatus:
    return ctx.cloud.status()


class RobotRef(BaseModel):
    robot: str = Field(description="Robot ID or unambiguous name")


class CloudPasswordResult(KitModel):
    outcome: Literal["stored", "unavailable"]
    reason: str | None = Field(
        default=None,
        description="Why not: a cloud status reason, `not_in_account`, or `auth_failed` when "
        "the robot rejected the password the account gave",
    )


@operation(
    description="Fetch a robot's local password from the iRobot account, check it by "
    "connecting, and store it. For models or robots that won't release it locally. The "
    "password is never returned.",
    params=RobotRef,
    result=CloudPasswordResult,
    http=Http("POST", "/robots/{robot}/password/cloud"),
    cli=Cli(("cloud",), "password", args=("robot",)),
)
async def cloud_retrieve_password(ctx: ServiceContext, params: RobotRef) -> CloudPasswordResult:
    session = ctx.resolve_robot(params.robot)
    try:
        password = await manager(ctx).robot_password(session.robot_id)
    except KitError as err:
        if err.code in _CLOUD_ERRORS:
            return CloudPasswordResult(outcome="unavailable", reason=_reason(err))
        raise
    if not password:
        return CloudPasswordResult(outcome="unavailable", reason="not_in_account")
    if await store_verified_password(ctx, session, password) is ConnectionState.AUTH_FAILED:
        return CloudPasswordResult(outcome="unavailable", reason="auth_failed")
    return CloudPasswordResult(outcome="stored")
