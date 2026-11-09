"""Reading and writing robot settings, confirmed by read-back (FR-033–FR-035, research R9).

A write publishes the setting's whole protocol `group` at once, inside the robot's FIFO queue
(FR-030), then waits for the robot to report the requested value. Only a read-back counts as
`applied` (FR-034): a robot that accepts a write and drops it reads back `not_applied`.
"""

import asyncio
from typing import Any

from roomba_kit.core.capabilities import Capability
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import SettingChange
from roomba_kit.profiles.base import SettingMapping
from roomba_kit.service.session import RobotSession

CONFIRM_TIMEOUT_S = 10.0
"""How long a write waits for the robot to report the new value."""

SettingValue = bool | int | str

CHOICES: dict[str, tuple[SettingValue, ...]] = {
    "passes": ("auto", "one", "two"),
    "edge_clean": (True, False),
    "always_finish": (True, False),
    "child_lock": (True, False),
    "eco_charge": (True, False),
    "schedule_hold": (True, False),
    "pad_wetness": (1, 2, 3),
    "spray_overlap": ("standard", "deep", "extended"),
}
"""Every setting the kit knows and its allowed values (contracts/operations.md)."""

_TRUE = {"true", "on", "yes", "1"}
_FALSE = {"false", "off", "no", "0"}


def coerce(setting: str, value: Any) -> SettingValue:
    """`value` as the setting's type. The CLI sends strings, so `"2"` and `"off"` are accepted."""
    choices = CHOICES[setting]
    result: Any = value
    if isinstance(choices[0], bool):
        if isinstance(value, str) and value.lower() in _TRUE | _FALSE:
            result = value.lower() in _TRUE
    elif isinstance(choices[0], int):
        if isinstance(value, str) and value.strip().isdigit():
            result = int(value)
    elif isinstance(value, str):
        result = value.lower()
    # `True == 1`, so match on type as well as value.
    if any(type(result) is type(c) and result == c for c in choices):
        return result  # type: ignore[no-any-return]
    raise KitError(
        ErrorCode.INVALID_REQUEST,
        f"Invalid value {value!r} for {setting}.",
        details={"setting": setting, "valid": list(choices)},
    )


def mapping_for(session: RobotSession, setting: str) -> SettingMapping:
    """The model's mapping for `setting`: `invalid_request` for an unknown setting, and
    `not_supported` for one the model lacks, both before any robot contact."""
    if setting not in CHOICES:
        raise KitError(
            ErrorCode.INVALID_REQUEST,
            f"Unknown setting: {setting}",
            session.robot_id,
            {"valid": sorted(CHOICES)},
        )
    profile = session.require(Capability.for_setting(setting))
    mapping = profile.settings.get(setting)
    if mapping is None:
        raise KitError(
            ErrorCode.NOT_SUPPORTED,
            f"{profile.display_name} declares {setting} but has no mapping for it.",
            session.robot_id,
            {"profile": profile.id, "capability": Capability.for_setting(setting).value},
        )
    return mapping


def read_settings(session: RobotSession) -> dict[str, Any]:
    """`{setting: value}` for every setting the model supports; `None` when not reported."""
    if session.profile is None:
        raise KitError(
            ErrorCode.UNSUPPORTED_MODEL,
            "This robot's model has no profile, so its settings can't be read.",
            session.robot_id,
        )
    if not session.reported:
        session.require_link()  # nothing reported yet: say why the robot can't be read
    return {name: m.read(session.reported) for name, m in session.profile.settings.items()}


async def change_setting(
    session: RobotSession, setting: str, value: Any, timeout_s: float | None = None
) -> SettingChange:
    """Write one logical setting and confirm it by read-back (FR-034, FR-035)."""
    mapping = mapping_for(session, setting)
    wanted = coerce(setting, value)
    window = CONFIRM_TIMEOUT_S if timeout_s is None else timeout_s

    async def run() -> SettingChange:
        session.require_link()
        seq = session.change_seq
        await session.publish_settings(mapping.write(wanted))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + window
        while (left := deadline - loop.time()) > 0:
            await session.wait_change(seq, left)
            if session.change_seq > seq:
                seq = session.change_seq
                if mapping.confirm(session.reported, wanted):
                    break
        # A value the robot already held may never be echoed; it still reads back correctly.
        applied = mapping.confirm(session.reported, wanted)
        return SettingChange(
            robot_id=session.robot_id,
            setting=setting,
            requested=wanted,
            outcome="applied" if applied else "not_applied",
            actual=mapping.read(session.reported),
        )

    return await session.submit(run)
