"""The `verify` operation: scripted hardware checks that close unverified capabilities."""

from pydantic import BaseModel, Field

from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service import verify as checks
from roomba_kit.service.app import ServiceContext


class VerifyParams(BaseModel):
    robot: str = Field(description="Robot ID or name")
    capabilities: list[str] | None = Field(
        default=None,
        description="Capabilities to check, e.g. `setting.passes find`. Default: every one the "
        "model declares (`train` is only ever run when named here)",
    )
    allow_motion: bool = Field(
        default=False,
        description="Run checks that act on the robot (every command except `find`). Without "
        "it they are skipped",
    )


@operation(
    description="Check capabilities against the real robot and record the run: settings are "
    "written, read back and restored; commands are issued and confirmed (only `find` without "
    "allow_motion). Returns each result with evidence, plus a markdown report for "
    "`docs/verification/<profile>.md`.",
    params=VerifyParams,
    result=checks.VerificationRun,
    motion=True,
    http=Http("POST", "/robots/{robot}/verify"),
    cli=Cli((), "verify", args=("robot", "capabilities")),
)
async def verify(ctx: ServiceContext, params: VerifyParams) -> checks.VerificationRun:
    session = ctx.resolve_robot(params.robot)
    return await checks.verify(ctx, session, params.capabilities, params.allow_motion)
