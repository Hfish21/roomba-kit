"""Capabilities a model profile can declare, each with a verification status."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Capability(StrEnum):
    # Commands
    CLEAN = "clean"
    CLEAN_ROOMS = "clean_rooms"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    DOCK = "dock"
    FIND = "find"
    EVAC = "evac"
    REBOOT = "reboot"
    TRAIN = "train"
    # Settings
    SETTING_PASSES = "setting.passes"
    SETTING_EDGE_CLEAN = "setting.edge_clean"
    SETTING_ALWAYS_FINISH = "setting.always_finish"
    SETTING_CHILD_LOCK = "setting.child_lock"
    SETTING_ECO_CHARGE = "setting.eco_charge"
    SETTING_SCHEDULE_HOLD = "setting.schedule_hold"
    SETTING_PAD_WETNESS = "setting.pad_wetness"
    SETTING_SPRAY_OVERLAP = "setting.spray_overlap"
    SETTING_CARPET_BOOST = "setting.carpet_boost"
    # Telemetry
    STATE_BIN = "state.bin"
    STATE_CLEAN_BASE = "state.clean_base"
    STATE_PAD = "state.pad"
    STATE_TANK = "state.tank"
    STATE_LID = "state.lid"
    # Live data
    LIVE_POSITION = "live_position"
    # Cloud
    CLOUD_ROOMS = "cloud.rooms"
    CLOUD_HISTORY = "cloud.history"
    CLOUD_COVERAGE = "cloud.coverage"
    CLOUD_TIMELINE = "cloud.timeline"

    @classmethod
    def for_setting(cls, setting: str) -> "Capability":
        """`passes` -> `Capability.SETTING_PASSES`. Raises ValueError for unknown settings."""
        return cls(f"setting.{setting}")


COMMAND_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.CLEAN,
        Capability.CLEAN_ROOMS,
        Capability.PAUSE,
        Capability.RESUME,
        Capability.STOP,
        Capability.DOCK,
        Capability.FIND,
        Capability.EVAC,
        Capability.REBOOT,
        Capability.TRAIN,
    }
)


class Verification(BaseModel):
    """Whether a capability has been exercised on real hardware (Constitution VIII)."""

    model_config = ConfigDict(frozen=True)

    status: Literal["verified", "unverified"]
    run: str | None = None

    @classmethod
    def verified(cls, run: str) -> "Verification":
        if not run:
            raise ValueError("a verified capability must cite the hardware run that proved it")
        return cls(status="verified", run=run)

    @classmethod
    def unverified(cls) -> "Verification":
        return cls(status="unverified")

    @property
    def is_verified(self) -> bool:
        return self.status == "verified"


class CapabilityStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    capability: Capability
    verification: Verification
