import pytest

from roomba_kit.core.capabilities import Capability as C
from roomba_kit.profiles import braava_m6, roomba_i5
from roomba_kit.profiles.loader import load_profiles, match_profile

RUN = "session b10c0fbc 2026-11-08"

EXPECTED = {
    "roomba_i5": (
        roomba_i5.PROFILE,
        {
            C.CLEAN, C.PAUSE, C.RESUME, C.DOCK, C.FIND, C.CLEAN_ROOMS, C.EVAC,
            C.STATE_BIN, C.STATE_CLEAN_BASE,
            C.CLOUD_ROOMS, C.CLOUD_HISTORY, C.CLOUD_COVERAGE, C.CLOUD_TIMELINE,
        },
        {
            C.STOP, C.REBOOT, C.TRAIN,
            C.SETTING_PASSES, C.SETTING_EDGE_CLEAN, C.SETTING_ALWAYS_FINISH,
            C.SETTING_CHILD_LOCK, C.SETTING_ECO_CHARGE, C.SETTING_SCHEDULE_HOLD,
        },
    ),
    "braava_m6": (
        braava_m6.PROFILE,
        {
            C.CLEAN, C.PAUSE, C.RESUME, C.DOCK, C.FIND,
            C.STATE_PAD, C.STATE_TANK, C.STATE_LID, C.LIVE_POSITION,
            C.CLOUD_ROOMS, C.CLOUD_HISTORY, C.CLOUD_COVERAGE,
        },
        {
            C.STOP, C.REBOOT, C.TRAIN, C.CLEAN_ROOMS, C.CLOUD_TIMELINE,
            C.SETTING_PASSES, C.SETTING_EDGE_CLEAN, C.SETTING_CHILD_LOCK,
            C.SETTING_ECO_CHARGE, C.SETTING_SCHEDULE_HOLD,
            C.SETTING_PAD_WETNESS, C.SETTING_SPRAY_OVERLAP,
        },
    ),
}  # fmt: skip


@pytest.mark.parametrize("profile_id", EXPECTED)
def test_capabilities_and_verification(profile_id):
    profile, verified, unverified = EXPECTED[profile_id]
    assert profile.id == profile_id
    assert set(profile.capabilities) == verified | unverified
    for cap in verified:
        assert profile.capabilities[cap].is_verified, cap
        assert profile.capabilities[cap].run == RUN
    for cap in unverified:
        assert not profile.capabilities[cap].is_verified, cap
    assert profile.settings == {}
    assert profile.error_texts == {} and profile.not_ready_texts == {}


def test_i5_omits_carpet_boost_and_live_position():
    caps = roomba_i5.PROFILE.capabilities
    assert C.SETTING_CARPET_BOOST not in caps and C.LIVE_POSITION not in caps


def test_entry_points_load_and_match():
    load_profiles.cache_clear()
    try:
        profiles = load_profiles()
        assert profiles["roomba_i5"] is roomba_i5.PROFILE
        assert profiles["braava_m6"] is braava_m6.PROFILE
        assert match_profile("i5G5020") is roomba_i5.PROFILE
        assert match_profile("m611020") is braava_m6.PROFILE
        assert match_profile("j7158") is None
    finally:
        load_profiles.cache_clear()
