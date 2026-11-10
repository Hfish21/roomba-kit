"""Replay each shipped profile's scrubbed state fixture (contracts/model-profile.md, rule 5).

Profiles are discovered through the entry-point group, so a new profile without a fixture
and an `EXPECTED` entry here fails `pytest tests/profiles`.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from roomba_kit.core.capabilities import Capability as C
from roomba_kit.core.models import Activity
from roomba_kit.profiles.loader import load_profiles, match_profile
from roomba_kit.service.normalize import normalize

FIXTURES = Path(__file__).parents[1] / "fixtures" / "profiles"
AT = datetime(2026, 11, 8, 12, tzinfo=UTC)
ROBOT_ID = "0" * 31 + "1"

V, U = "verified", "unverified"

EXPECTED: dict[str, dict[str, Any]] = {
    "roomba_i5": {
        "capabilities": {
            C.CLEAN: V, C.PAUSE: V, C.RESUME: V, C.DOCK: V, C.FIND: V, C.CLEAN_ROOMS: V,
            C.EVAC: V, C.STATE_BIN: V, C.STATE_CLEAN_BASE: V,
            C.CLOUD_ROOMS: V, C.CLOUD_HISTORY: V, C.CLOUD_COVERAGE: V, C.CLOUD_TIMELINE: V,
            C.STOP: V, C.REBOOT: V, C.TRAIN: U,
            C.SETTING_PASSES: V, C.SETTING_EDGE_CLEAN: V, C.SETTING_ALWAYS_FINISH: V,
            C.SETTING_CHILD_LOCK: V, C.SETTING_ECO_CHARGE: V, C.SETTING_SCHEDULE_HOLD: V,
        },
        "state": {
            "battery_pct": 100,
            "activity": Activity.DOCKED,
            "mission": None,
            "error": None,
            "not_ready": None,
            "signal": {"rssi": -39, "snr": 24},
            "map": {"map_id": "FbKwAujERSazAALZRD_rEg", "version": "231231T204559"},
            "last_command": ("clean_rooms", ["7"]),
            "schedules": 5,
            "counters": {
                "missions_ok": 264, "missions_failed": 186, "missions_cancelled": 100,
                "runtime_hours": 680, "stuck_count": 172,
            },
            "bin": {"present": True, "full": False},
            "clean_base": {"present": True, "state": 301},
            "pad": None,
            "tank": None,
            "lid_open": None,
        },
    },
    "braava_m6": {
        "capabilities": {
            C.CLEAN: V, C.PAUSE: V, C.RESUME: V, C.DOCK: V, C.FIND: V,
            C.STATE_PAD: V, C.STATE_TANK: V, C.STATE_LID: V, C.LIVE_POSITION: V,
            C.CLOUD_ROOMS: V, C.CLOUD_HISTORY: V, C.CLOUD_COVERAGE: V,
            C.STOP: V, C.REBOOT: V, C.TRAIN: U, C.CLEAN_ROOMS: V, C.CLOUD_TIMELINE: V,
            C.SETTING_PASSES: V, C.SETTING_EDGE_CLEAN: V, C.SETTING_CHILD_LOCK: V,
            C.SETTING_ECO_CHARGE: V, C.SETTING_SCHEDULE_HOLD: V,
            C.SETTING_PAD_WETNESS: V, C.SETTING_SPRAY_OVERLAP: V,
        },
        "state": {
            "battery_pct": 90,
            "activity": Activity.CHARGING,
            "mission": None,
            "error": None,
            "not_ready": None,
            "signal": {"rssi": -49, "snr": 29},
            "map": {"map_id": "5brjBHI-RtKr1Yt7JzFjDw", "version": "260128T162258"},
            "last_command": ("find", []),
            "schedules": 1,
            "counters": {
                "missions_ok": 22, "missions_failed": 134, "missions_cancelled": 23,
                "runtime_hours": None, "stuck_count": 70,
            },
            "bin": None,
            "clean_base": None,
            "pad": {"detected": "reusableWet"},
            "tank": {"present": True, "level_pct": 100},
            "lid_open": False,
        },
    },
}  # fmt: skip

SHIPPED = sorted(load_profiles())


def _fixture(profile_id: str) -> dict[str, Any]:
    return json.loads((FIXTURES / profile_id / "state.json").read_text())


def _summary(state: Any) -> dict[str, Any]:
    def dump(value: Any) -> Any:
        return None if value is None else value.model_dump(mode="json")

    return {
        "battery_pct": state.battery_pct,
        "activity": state.activity,
        "mission": dump(state.mission),
        "error": dump(state.error),
        "not_ready": dump(state.not_ready),
        "signal": dump(state.signal),
        "map": dump(state.map),
        "last_command": (state.last_command.command, state.last_command.room_ids),
        "schedules": len(state.schedules),
        "counters": dump(state.counters),
        "bin": dump(state.bin),
        "clean_base": dump(state.clean_base),
        "pad": dump(state.pad),
        "tank": dump(state.tank),
        "lid_open": state.lid_open,
    }


def test_every_shipped_profile_has_a_fixture_and_expectations():
    assert SHIPPED, "no profiles installed"
    for profile_id in SHIPPED:
        assert (FIXTURES / profile_id / "state.json").is_file(), f"{profile_id}: no fixture"
        assert profile_id in EXPECTED, f"{profile_id}: add its expectations to EXPECTED"


@pytest.mark.parametrize("profile_id", SHIPPED)
def test_fixture_sku_matches_its_profile(profile_id):
    assert match_profile(_fixture(profile_id)["sku"]) is load_profiles()[profile_id]


@pytest.mark.parametrize("profile_id", SHIPPED)
def test_capabilities_and_verification(profile_id):
    profile = load_profiles()[profile_id]
    declared = {s.capability: s.verification.status for s in profile.capability_statuses()}
    assert declared == EXPECTED[profile_id]["capabilities"]
    for status in profile.capability_statuses():
        if status.verification.is_verified:
            assert status.verification.run, f"{status.capability} is verified without a run"


@pytest.mark.parametrize("profile_id", SHIPPED)
def test_fixture_replays_to_the_expected_state(profile_id):
    profile = load_profiles()[profile_id]
    state = normalize(ROBOT_ID, _fixture(profile_id), profile, AT)
    assert (state.robot_id, state.at) == (ROBOT_ID, AT)
    assert _summary(state) == EXPECTED[profile_id]["state"]
    assert set(state.settings) == set(profile.settings)


@pytest.mark.parametrize("profile_id", SHIPPED)
def test_undeclared_telemetry_is_never_reported(profile_id):
    """A field gated on a capability is None whenever the profile doesn't declare it."""
    profile = load_profiles()[profile_id]
    state = normalize(ROBOT_ID, _fixture(profile_id), profile, AT)
    gated = {
        C.STATE_BIN: state.bin,
        C.STATE_CLEAN_BASE: state.clean_base,
        C.STATE_PAD: state.pad,
        C.STATE_TANK: state.tank,
        C.STATE_LID: state.lid_open,
    }
    for capability, value in gated.items():
        assert (value is not None) == profile.supports(capability), capability
