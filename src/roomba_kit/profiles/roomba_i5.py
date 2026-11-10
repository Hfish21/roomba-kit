"""Roomba i5/i5+ (SKU `i5…`, firmware `daredevil`)."""

from collections.abc import Callable, Mapping
from typing import Any

from roomba_kit.core.capabilities import Capability as C
from roomba_kit.core.capabilities import Verification
from roomba_kit.profiles.base import ModelProfile, Reported, SettingMapping

_DOC = "docs/verification/roomba_i5.md#"


def _hw(anchor: str) -> Verification:
    """Verified on hardware; cites the run in this profile's verification page."""
    return Verification.verified(_DOC + anchor)


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
        C.CLEAN: _hw("run-20261109-185420"),
        C.PAUSE: _hw("run-20261109-185420"),
        C.RESUME: _hw("run-20261109-185420"),
        C.STOP: _hw("run-20261109-185420"),
        C.DOCK: _hw("run-20261109-211438"),
        C.FIND: _hw("run-20261109-141750"),
        C.EVAC: _hw("run-20261109-142228"),
        C.REBOOT: _hw("run-20261109-194131"),
        C.CLEAN_ROOMS: _hw("hw1-laundry-room-clean"),
        C.STATE_BIN: _hw("run-20261109-141750"),
        C.STATE_CLEAN_BASE: _hw("run-20261109-141750"),
        C.SETTING_PASSES: _hw("run-20261109-141750"),
        C.SETTING_EDGE_CLEAN: _hw("run-20261109-141750"),
        C.SETTING_ALWAYS_FINISH: _hw("run-20261109-141750"),
        C.SETTING_CHILD_LOCK: _hw("run-20261109-141750"),
        C.SETTING_ECO_CHARGE: _hw("run-20261109-141750"),
        C.SETTING_SCHEDULE_HOLD: _hw("run-20261109-141750"),
        C.CLOUD_ROOMS: _hw("run-20261109-141750"),
        C.CLOUD_HISTORY: _hw("run-20261109-141750"),
        C.CLOUD_COVERAGE: _hw("hw1-cloud-coverage"),
        C.CLOUD_TIMELINE: _hw("hw1-laundry-room-clean"),
        C.TRAIN: _UNVERIFIED,  # needs the owner's OK: it can re-create the map
    },
    # HW-1 (docs/verification/roomba_i5.md): `dock` sent while cleaning was echoed but
    # ignored; from `paused` it worked.
    dock_needs_pause=True,
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
