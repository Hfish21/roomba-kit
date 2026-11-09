"""Service operations: health, status and the redacted config view (FR-011, FR-014)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from roomba_kit.config.settings import (
    AlertsConfig,
    DiscoveryConfig,
    EventsConfig,
    HistoryConfig,
    RobotEntry,
    ServerConfig,
)
from roomba_kit.core.models import ConnectionState, KitModel
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service.app import CloudStatus, ServiceContext

Redacted = Literal["***"] | None


class Health(KitModel):
    status: Literal["ok"] = "ok"
    version: str


@operation(
    description="Whether the service is up, and its version. Needs no API token over HTTP.",
    result=Health,
    read_only=True,
    http=Http("GET", "/health", auth=False),
    cli=Cli((), "health"),
)
async def health(ctx: ServiceContext, params: BaseModel) -> Health:
    return Health(version=ctx.version)


class RobotConnection(KitModel):
    id: str
    connection: ConnectionState


class ServiceStatus(KitModel):
    version: str
    uptime_s: int
    robots: list[RobotConnection]
    cloud: CloudStatus


@operation(
    description="The service's version and uptime, each robot's connection state, and the "
    "cloud subsystem's status.",
    result=ServiceStatus,
    read_only=True,
    http=Http("GET", "/service_status"),
    cli=Cli(("service",), "status"),
)
async def service_status(ctx: ServiceContext, params: BaseModel) -> ServiceStatus:
    return ServiceStatus(
        version=ctx.version,
        uptime_s=ctx.uptime_s,
        robots=[RobotConnection(id=s.robot_id, connection=s.connection) for s in ctx],
        cloud=ctx.cloud.status(),
    )


class RobotView(RobotEntry):
    password: Redacted = Field(description='`"***"` when a password is stored, else null')


class CloudView(BaseModel):
    enabled: bool
    country: str
    username: Redacted = None
    password: Redacted = None


class TokenView(BaseModel):
    name: str
    created_at: datetime


class ConfigView(KitModel):
    server: ServerConfig
    alerts: AlertsConfig
    history: HistoryConfig
    events: EventsConfig
    discovery: DiscoveryConfig
    cloud: CloudView
    robots: list[RobotView]
    tokens: list[TokenView] = Field(description="Names only; token values are never shown")


def _mask(value: object) -> Redacted:
    return "***" if value else None


@operation(
    description="The effective configuration (files plus environment overrides), with every "
    'secret shown as `"***"`.',
    result=ConfigView,
    read_only=True,
    http=Http("GET", "/config_show"),
    cli=Cli(("config",), "show"),
)
async def config_show(ctx: ServiceContext, params: BaseModel) -> ConfigView:
    settings, creds = ctx.settings, ctx.credentials
    cloud = creds.cloud()
    return ConfigView(
        server=settings.server,
        alerts=settings.alerts,
        history=settings.history,
        events=settings.events,
        discovery=settings.discovery,
        cloud=CloudView(
            enabled=settings.cloud.enabled,
            country=settings.cloud.country,
            username=_mask(cloud and cloud.username),
            password=_mask(cloud and cloud.password),
        ),
        robots=[
            RobotView(**r.model_dump(), password=_mask(creds.robot_password(r.id)))
            for r in settings.robots
        ],
        tokens=[TokenView(name=t.name, created_at=t.created_at) for t in ctx.tokens.list()],
    )
