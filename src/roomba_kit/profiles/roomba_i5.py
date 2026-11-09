"""Roomba i5/i5+ (SKU `i5…`, firmware `daredevil`)."""

from collections.abc import Callable, Mapping
from typing import Any

from roomba_kit.core.capabilities import Capability as C
from roomba_kit.core.capabilities import Verification
from roomba_kit.profiles.base import ModelProfile, Reported, SettingMapping

_VERIFIED = Verification.verified("session b10c0fbc 2026-11-08")
_UNVERIFIED = Verification.unverified()


def _holds(reported: Reported, written: Mapping[str, Any]) -> bool:
    """True when `reported` holds every written key (nested dicts compared key by key)."""
    for key, want in written.items():
        have = reported.get(key)
        if isinstance(want, Mapping):
            if not isinstance(have, Mapping) or not _holds(have, want):
                return False
        elif have != want:
            return False
    return True


def _mapping(
    read: Callable[[Reported], Any], write: Callable[[Any], dict[str, Any]], *group: str
) -> SettingMapping:
    return SettingMapping(
        read=read, write=write, group=group, confirm=lambda r, v: _holds(r, write(v))
    )


def _flag(key: str, *, inverted: bool = False) -> SettingMapping:
    """A boolean setting stored in one key, optionally inverted (`edge_clean = not openOnly`)."""

    def read(r: Reported) -> bool | None:
        value = r.get(key)
        return None if value is None else bool(value) != inverted

    return _mapping(read, lambda v: {key: bool(v) != inverted}, key)


_PASSES = {"auto": (False, False), "one": (True, False), "two": (True, True)}
"""`passes` -> (`noAutoPasses`, `twoPass`). They are always written together (FR-035)."""


def _read_passes(r: Reported) -> str | None:
    no_auto, two = r.get("noAutoPasses"), r.get("twoPass")
    if no_auto is None or two is None:
        return None
    return ("two" if two else "one") if no_auto else "auto"


def _write_passes(value: Any) -> dict[str, Any]:
    no_auto, two = _PASSES[value]
    return {"noAutoPasses": no_auto, "twoPass": two}


PROFILE = ModelProfile(
    id="roomba_i5",
    display_name="Roomba i5/i5+",
    sku_patterns=[r"^i5"],
    capabilities={
        **dict.fromkeys(
            (
                C.CLEAN,
                C.PAUSE,
                C.RESUME,
                C.DOCK,
                C.FIND,
                C.CLEAN_ROOMS,
                C.EVAC,
                C.STATE_BIN,
                C.STATE_CLEAN_BASE,
                C.CLOUD_ROOMS,
                C.CLOUD_HISTORY,
                C.CLOUD_COVERAGE,
                C.CLOUD_TIMELINE,
            ),
            _VERIFIED,
        ),
        **dict.fromkeys(
            (
                C.STOP,
                C.REBOOT,
                C.TRAIN,
                C.SETTING_PASSES,
                C.SETTING_EDGE_CLEAN,
                C.SETTING_ALWAYS_FINISH,
                C.SETTING_CHILD_LOCK,
                C.SETTING_ECO_CHARGE,
                C.SETTING_SCHEDULE_HOLD,
            ),
            _UNVERIFIED,
        ),
    },
    settings={
        "passes": _mapping(_read_passes, _write_passes, "noAutoPasses", "twoPass"),
        "edge_clean": _flag("openOnly", inverted=True),
        "always_finish": _flag("binPause", inverted=True),
        "child_lock": _flag("childLock"),
        "eco_charge": _flag("ecoCharge"),
        "schedule_hold": _flag("schedHold"),
        # No `carpet_boost`: the i5 drops `carpetBoost`/`vacHigh` writes.
    },
)
