"""Roomba i5/i5+ (SKU `i5…`, firmware `daredevil`). Settings mappings land in T070."""

from roomba_kit.core.capabilities import Capability as C
from roomba_kit.core.capabilities import Verification
from roomba_kit.profiles.base import ModelProfile

_VERIFIED = Verification.verified("session b10c0fbc 2026-11-08")
_UNVERIFIED = Verification.unverified()

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
)
