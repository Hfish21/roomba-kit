"""T070/T071: each profile's settings mappings, and value coercion (contracts/operations.md)."""

import pytest

from roomba_kit.core.capabilities import Capability
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.profiles import braava_m6, roomba_i5
from roomba_kit.service.settings import CHOICES, coerce

I5 = roomba_i5.PROFILE.settings
M6 = braava_m6.PROFILE.settings
PROFILES = [roomba_i5.PROFILE, braava_m6.PROFILE]


def test_declared_settings():
    assert set(I5) == {
        "passes",
        "edge_clean",
        "always_finish",
        "child_lock",
        "eco_charge",
        "schedule_hold",
    }
    assert set(M6) == {
        "passes",
        "edge_clean",
        "child_lock",
        "eco_charge",
        "schedule_hold",
        "pad_wetness",
        "spray_overlap",
    }
    assert "carpet_boost" not in I5  # the i5 drops carpetBoost/vacHigh writes


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.id)
def test_every_mapping_has_a_capability_and_a_round_trip(profile):
    for name, mapping in profile.settings.items():
        assert Capability.for_setting(name) in profile.capabilities
        for value in CHOICES[name]:
            written = mapping.write(value)
            assert set(written) == set(mapping.group), name
            assert mapping.read(written) == value, (name, value)
            assert mapping.confirm(written, value), (name, value)


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.id)
def test_reads_tolerate_missing_keys(profile):
    for name, mapping in profile.settings.items():
        assert mapping.read({}) is None, name
        assert not mapping.confirm({}, CHOICES[name][0]), name


@pytest.mark.parametrize(
    ("value", "keys"),
    [
        ("auto", {"noAutoPasses": False, "twoPass": False}),
        ("one", {"noAutoPasses": True, "twoPass": False}),
        ("two", {"noAutoPasses": True, "twoPass": True}),
    ],
)
def test_passes_writes_both_keys(value, keys):
    assert I5["passes"].write(value) == keys
    assert M6["passes"].write(value) == keys


def test_passes_needs_both_keys_to_confirm():
    # The i3-style drop: one key of the pair applied, the other not.
    half = {"noAutoPasses": True, "twoPass": False}
    assert not I5["passes"].confirm(half, "two")
    assert I5["passes"].read({"noAutoPasses": True}) is None
    assert I5["passes"].read({"noAutoPasses": False, "twoPass": True}) == "auto"


def test_inverted_flags():
    assert I5["edge_clean"].write(False) == {"openOnly": True}
    assert I5["edge_clean"].read({"openOnly": False}) is True
    assert I5["always_finish"].write(True) == {"binPause": False}
    assert I5["always_finish"].read({"binPause": True}) is False
    assert I5["schedule_hold"].write(True) == {"schedHold": True}


def test_mop_settings():
    assert M6["pad_wetness"].write(2) == {"padWetness": {"disposable": 2, "reusable": 2}}
    assert M6["pad_wetness"].read({"padWetness": {"disposable": 1, "reusable": 3}}) is None
    reported = {"padWetness": {"disposable": 2, "reusable": 2, "other": 9}}
    assert M6["pad_wetness"].confirm(reported, 2)
    assert {v: M6["spray_overlap"].write(v)["rankOverlap"] for v in CHOICES["spray_overlap"]} == {
        "standard": 67,
        "deep": 85,
        "extended": 25,
    }
    assert M6["spray_overlap"].read({"rankOverlap": 15}) is None


@pytest.mark.parametrize(
    ("setting", "raw", "value"),
    [
        ("child_lock", "true", True),
        ("child_lock", "OFF", False),
        ("child_lock", False, False),
        ("pad_wetness", "3", 3),
        ("pad_wetness", 1, 1),
        ("passes", "Two", "two"),
        ("spray_overlap", "extended", "extended"),
    ],
)
def test_coerce(setting, raw, value):
    assert coerce(setting, raw) == value
    assert type(coerce(setting, raw)) is type(value)


@pytest.mark.parametrize(
    ("setting", "raw"),
    [("child_lock", "maybe"), ("child_lock", 1), ("pad_wetness", True), ("pad_wetness", "0")],
)
def test_coerce_rejects(setting, raw):
    with pytest.raises(KitError) as exc:
        coerce(setting, raw)
    assert exc.value.code is ErrorCode.INVALID_REQUEST
    assert exc.value.details["valid"] == list(CHOICES[setting])


def test_every_setting_capability_is_a_known_setting():
    """A `setting.*` capability missing here was "Unknown setting" instead of `not_supported`
    on a model lacking it."""
    declared = {
        c.value.removeprefix("setting.") for c in Capability if c.value.startswith("setting.")
    }
    assert set(CHOICES) == declared
