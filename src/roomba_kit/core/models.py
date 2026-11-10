"""Kit-owned data models (data-model.md). No transport-library types appear here."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from roomba_kit.core.capabilities import CapabilityStatus


class KitModel(BaseModel):
    """Base for every public model: `from_`-style fields serialize under their wire alias."""

    model_config = ConfigDict(
        serialize_by_alias=True, validate_by_name=True, validate_by_alias=True
    )


class ConnectionState(StrEnum):
    CONNECTED = "connected"
    CONNECTING = "connecting"
    OFFLINE = "offline"
    HELD_ELSEWHERE = "held_elsewhere"
    AUTH_FAILED = "auth_failed"
    NOT_CONFIGURED = "not_configured"


class Activity(StrEnum):
    DOCKED = "docked"
    CHARGING = "charging"
    CLEANING = "cleaning"
    MAPPING = "mapping"
    PAUSED = "paused"
    RETURNING = "returning"
    EMPTYING_BIN = "emptying_bin"
    STUCK = "stuck"
    UNKNOWN = "unknown"


class Initiator(StrEnum):
    KIT = "kit"
    SCHEDULE = "schedule"
    BUTTON = "button"
    APP = "app"
    TEAM = "team"
    OTHER = "other"


class MissionOutcome(StrEnum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    STUCK = "stuck"
    FAILED = "failed"
    MANUALLY_DOCKED = "manually_docked"
    UNKNOWN = "unknown"


TERMINAL_OUTCOMES: frozenset[MissionOutcome] = frozenset(MissionOutcome) - {
    MissionOutcome.IN_PROGRESS
}


ADD_A_MODEL_GUIDE = "docs/guides/adding-a-model.md"
_GUIDE = "For a model without a profile, the guide to adding one; otherwise null."


class Robot(KitModel):
    id: str = Field(description="The robot's permanent ID (BLID); the only identifier.")
    name: str
    profile_id: str | None
    supported: bool
    sku: str
    firmware: str
    address: str
    connection: ConnectionState
    capabilities: list[CapabilityStatus]
    guide: str | None = Field(default=None, description=_GUIDE)

    @model_validator(mode="after")
    def _point_at_guide(self) -> Self:
        if not self.supported and self.guide is None:
            self.guide = ADD_A_MODEL_GUIDE
        return self


class DiscoveredRobot(KitModel):
    id: str
    name: str
    sku: str
    firmware: str
    address: str
    supported: bool
    profile_id: str | None = None
    configured: bool = False
    guide: str | None = Field(default=None, description=_GUIDE)

    @model_validator(mode="after")
    def _point_at_guide(self) -> Self:
        if not self.supported and self.guide is None:
            self.guide = ADD_A_MODEL_GUIDE
        return self


class MissionRef(KitModel):
    id: str
    started_at: datetime | None = None
    initiator: Initiator


class CommandEcho(KitModel):
    """The robot's echo of the last command it accepted, from any client."""

    command: str
    at: datetime | None = None
    initiator: Initiator
    room_ids: list[str] = Field(default_factory=list)
    map_id: str | None = None
    map_version: str | None = None


class CodeText(KitModel):
    code: int
    text: str


class MapRef(KitModel):
    map_id: str
    version: str


class Signal(KitModel):
    rssi: int | None = None
    snr: int | None = None


class RobotSchedule(KitModel):
    """A schedule stored on the robot. Read-only in V1."""

    enabled: bool
    days: list[int] = Field(description="0 = Sunday … 6 = Saturday")
    hour: int
    minute: int
    command: str | None = None
    team: bool = Field(False, description="True when the entry triggers a vendor team routine")


class Counters(KitModel):
    missions_ok: int | None = None
    missions_failed: int | None = None
    missions_cancelled: int | None = None
    runtime_hours: int | None = None
    stuck_count: int | None = None


class BinState(KitModel):
    present: bool | None = None
    full: bool | None = None


class CleanBaseState(KitModel):
    present: bool
    state: int | None = None


class PadState(KitModel):
    detected: str | None = None


class TankState(KitModel):
    present: bool | None = None
    level_pct: int | None = None


class RobotState(KitModel):
    robot_id: str
    at: datetime
    battery_pct: int | None = None
    activity: Activity = Activity.UNKNOWN
    mission: MissionRef | None = None
    last_command: CommandEcho | None = None
    error: CodeText | None = None
    not_ready: CodeText | None = None
    signal: Signal | None = None
    map: MapRef | None = None
    schedules: list[RobotSchedule] = Field(default_factory=list)
    counters: Counters = Field(default_factory=Counters)
    bin: BinState | None = None
    clean_base: CleanBaseState | None = None
    pad: PadState | None = None
    tank: TankState | None = None
    lid_open: bool | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class Room(KitModel):
    robot_id: str
    map_id: str
    map_version: str
    room_id: str
    cloud_name: str | None = None
    local_name: str | None = None
    type: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def name(self) -> str:
        """Effective name: `local_name`, then `cloud_name`, then `Room <id>`."""
        return self.local_name or self.cloud_name or f"Room {self.room_id}"


class LearnedRoom(KitModel):
    robot_id: str
    map_id: str
    room_id: str
    first_seen: datetime
    last_seen: datetime


class StateSummary(KitModel):
    activity: Activity
    mission: MissionRef | None = None


class CommandResult(KitModel):
    robot_id: str
    command: str
    outcome: Literal["confirmed", "unconfirmed"]
    state: StateSummary
    waited_ms: int


class SettingChange(KitModel):
    robot_id: str
    setting: str
    requested: Any
    outcome: Literal["applied", "not_applied"]
    actual: Any


class Mission(KitModel):
    id: str
    robot_id: str
    started_at: datetime
    ended_at: datetime | None = None
    initiator: Initiator
    rooms_requested: list[str] = Field(default_factory=list, description="Empty = whole home")
    outcome: MissionOutcome = MissionOutcome.IN_PROGRESS
    duration_s: int | None = None
    errors: list[CodeText] = Field(default_factory=list)
    cloud_mission_ref: str | None = None


class ActivityTransition(KitModel):
    robot_id: str
    at: datetime
    from_: Activity = Field(alias="from")
    to: Activity
    mission_id: str | None = None


class MissionDetail(Mission):
    """One mission from local history, with its activity transitions."""

    transitions: list[ActivityTransition] = Field(
        default_factory=list, description="The mission's activity changes, oldest first"
    )


class PositionFix(KitModel):
    robot_id: str
    at: datetime
    x_m: float
    y_m: float
    heading_rad: float
    map_id: str | None = None
    map_version: str | None = None


class NoFix(KitModel):
    no_fix: Literal[True] = True


class CoverageRecord(KitModel):
    robot_id: str
    mission_ref: str
    map_id: str
    map_version: str
    points: list[tuple[float, float]]
    point_area_m2: float | None = None
    dirt: list[tuple[float, float]] = Field(default_factory=list)
    clutter: list[tuple[float, float]] = Field(default_factory=list)
    retrieved_at: datetime
