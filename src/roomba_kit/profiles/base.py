"""Model profile types (contracts/model-profile.md). A profile is data plus small mappings."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from roomba_kit.core.capabilities import Capability, CapabilityStatus, Verification
from roomba_kit.core.models import RobotState

DEFAULT_PROTOCOL_COMMANDS: dict[str, str] = {
    "clean": "start",
    "clean_rooms": "start",
    # HW-1: an i5 silently ignored `reboot`; the firmware command is `reset` (iRobot's app
    # logs "Sending reset command to reboot robot"; roombapy-prime CommandType.RESET).
    "reboot": "reset",
}
"""Kit command name -> protocol command, where they differ. Everything else is sent as-is."""

Reported = Mapping[str, Any]
"""A merged raw `state.reported` dict. Only profiles and transport adapters read its keys."""


@dataclass(frozen=True)
class SettingMapping:
    """How one logical setting maps to protocol keys.

    `write(value)` returns every key in `group`, so related keys are always published together
    (e.g. `noAutoPasses` + `twoPass` for `passes`).
    """

    read: Callable[[Reported], Any]
    write: Callable[[Any], dict[str, Any]]
    group: tuple[str, ...]
    confirm: Callable[[Reported, Any], bool]


@dataclass(frozen=True)
class ConfirmView:
    """What a confirmation predicate sees while waiting for a command to take effect."""

    sent_at: datetime
    before: RobotState | None
    now: RobotState
    reconnected_since_send: bool = False
    echoed_since_send: bool | None = None
    """True once a `lastCommand` echo message arrived after the send (None: not tracked).
    Unlike echo timestamps (whole seconds), this tells two same-second commands apart."""


@dataclass(frozen=True)
class CommandSpec:
    """A command is confirmed when `predicate` holds within `window_s` seconds of sending."""

    predicate: Callable[[ConfirmView], bool]
    window_s: float


@dataclass(frozen=True)
class ModelProfile:
    id: str
    display_name: str
    sku_patterns: list[str]
    capabilities: dict[Capability, Verification]
    settings: dict[str, SettingMapping] = field(default_factory=dict)
    command_overrides: dict[str, CommandSpec] = field(default_factory=dict)
    error_texts: dict[int, str] = field(default_factory=dict)
    not_ready_texts: dict[int, str] = field(default_factory=dict)
    protocol_commands: dict[str, str] = field(default_factory=dict)
    dock_needs_pause: bool = False
    command_windows: dict[str, float] = field(default_factory=dict)
    """Per-command confirmation windows (seconds), keeping the default predicate."""
    """The firmware ignores `dock` while actively cleaning; the kit pauses first."""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.id):
            raise ValueError(f"profile id {self.id!r} must be snake_case")
        for name in self.settings:
            cap = Capability.for_setting(name)
            if cap not in self.capabilities:
                raise ValueError(f"profile {self.id}: setting {name!r} needs capability {cap}")

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def protocol_command(self, command: str) -> str:
        """The command name this model's firmware expects for a kit command."""
        return self.protocol_commands.get(command) or DEFAULT_PROTOCOL_COMMANDS.get(
            command, command
        )

    def matches(self, sku: str) -> bool:
        return any(re.search(p, sku) for p in self.sku_patterns)

    def capability_statuses(self) -> list[CapabilityStatus]:
        return [
            CapabilityStatus(capability=c, verification=v)
            for c, v in sorted(self.capabilities.items(), key=lambda kv: kv[0].value)
        ]
