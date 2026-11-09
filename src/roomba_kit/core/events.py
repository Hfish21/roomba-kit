"""Event envelope and typed payloads (contracts/events.md, schema `roomba-kit.event/v1`)."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from roomba_kit.core.models import (
    Activity,
    CodeText,
    ConnectionState,
    KitModel,
    Mission,
    PositionFix,
    RobotState,
)

EVENT_SCHEMA: Literal["roomba-kit.event/v1"] = "roomba-kit.event/v1"


class EventType(StrEnum):
    STATE_CHANGED = "state.changed"
    ACTIVITY_CHANGED = "activity.changed"
    MISSION_STARTED = "mission.started"
    MISSION_FINISHED = "mission.finished"
    ROBOT_DOCKED = "robot.docked"
    ROBOT_STUCK = "robot.stuck"
    BIN_FULL = "bin.full"
    ERROR_RAISED = "error.raised"
    ERROR_CLEARED = "error.cleared"
    CONNECTION_CHANGED = "connection.changed"
    SETTING_CHANGED = "setting.changed"
    CLOUD_STATUS = "cloud.status"
    CLOUD_TIMELINE = "cloud.timeline"
    POSITION = "position"
    ALERT_DOCK_SEARCH_TIMEOUT = "alert.dock_search_timeout"


class Event(KitModel):
    schema_: Literal["roomba-kit.event/v1"] = Field(EVENT_SCHEMA, alias="schema")
    seq: int
    type: EventType
    robot_id: str | None = None
    at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class StateChangedData(KitModel):
    changed: list[str]
    state: RobotState


class ActivityChangedData(KitModel):
    from_: Activity = Field(alias="from")
    to: Activity
    mission_id: str | None = None


class MissionEventData(KitModel):
    mission: Mission


class RobotDockedData(KitModel):
    mission_id: str | None = None


class RobotStuckData(KitModel):
    error: CodeText | None = None


class BinFullData(KitModel):
    pass


class ErrorEventData(KitModel):
    kind: Literal["error", "not_ready"]
    code: int
    text: str


class ConnectionChangedData(KitModel):
    from_: ConnectionState = Field(alias="from")
    to: ConnectionState
    reason: str | None = None


class SettingChangedData(KitModel):
    setting: str
    from_: Any = Field(alias="from")
    to: Any


class CloudStatusData(KitModel):
    status: Literal["not_configured", "connected", "unavailable"]
    reason: str | None = None


class CloudTimelineData(KitModel):
    mission_id: str | None = None
    kind: Literal["start", "travel", "room", "traversal", "evac", "fin", "other"]
    room_id: str | None = None
    dest: str | None = None
    raw_type: str


class DockSearchTimeoutData(KitModel):
    mission_id: str | None = None
    minutes: int


EVENT_DATA: dict[EventType, type[BaseModel]] = {
    EventType.STATE_CHANGED: StateChangedData,
    EventType.ACTIVITY_CHANGED: ActivityChangedData,
    EventType.MISSION_STARTED: MissionEventData,
    EventType.MISSION_FINISHED: MissionEventData,
    EventType.ROBOT_DOCKED: RobotDockedData,
    EventType.ROBOT_STUCK: RobotStuckData,
    EventType.BIN_FULL: BinFullData,
    EventType.ERROR_RAISED: ErrorEventData,
    EventType.ERROR_CLEARED: ErrorEventData,
    EventType.CONNECTION_CHANGED: ConnectionChangedData,
    EventType.SETTING_CHANGED: SettingChangedData,
    EventType.CLOUD_STATUS: CloudStatusData,
    EventType.CLOUD_TIMELINE: CloudTimelineData,
    EventType.POSITION: PositionFix,
    EventType.ALERT_DOCK_SEARCH_TIMEOUT: DockSearchTimeoutData,
}


def encode_data(type_: EventType, data: BaseModel) -> dict[str, Any]:
    """Validate a payload against its type's model and dump it to wire JSON."""
    model = EVENT_DATA[type_]
    if not isinstance(data, model):
        raise TypeError(f"{type_.value} expects {model.__name__}, got {type(data).__name__}")
    return data.model_dump(mode="json", by_alias=True)
