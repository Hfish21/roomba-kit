"""Braava jet m6 (SKU `m6…`, firmware `sanmarino`). Settings mappings land in T071."""

from roomba_kit.core.capabilities import Capability as C
from roomba_kit.core.capabilities import Verification
from roomba_kit.profiles.base import ModelProfile

_VERIFIED = Verification.verified("session b10c0fbc 2026-11-08")
_UNVERIFIED = Verification.unverified()

PROFILE = ModelProfile(
    id="braava_m6",
    display_name="Braava jet m6",
    sku_patterns=[r"^m6"],
    capabilities={
        **dict.fromkeys(
            (
                C.CLEAN,
                C.PAUSE,
                C.RESUME,
                C.DOCK,
                C.FIND,
                C.STATE_PAD,
                C.STATE_TANK,
                C.STATE_LID,
                C.LIVE_POSITION,
                C.CLOUD_ROOMS,
                C.CLOUD_HISTORY,
                C.CLOUD_COVERAGE,
            ),
            _VERIFIED,
        ),
        **dict.fromkeys(
            (
                C.STOP,
                C.REBOOT,
                C.TRAIN,
                C.CLEAN_ROOMS,
                C.CLOUD_TIMELINE,
                C.SETTING_PASSES,
                C.SETTING_EDGE_CLEAN,
                C.SETTING_CHILD_LOCK,
                C.SETTING_ECO_CHARGE,
                C.SETTING_SCHEDULE_HOLD,
                C.SETTING_PAD_WETNESS,
                C.SETTING_SPRAY_OVERLAP,
            ),
            _UNVERIFIED,
        ),
    },
)
