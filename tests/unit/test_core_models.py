import pytest

from roomba_kit.core.capabilities import Capability, Verification
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import EVENT_DATA, BinFullData, EventType, encode_data
from roomba_kit.core.models import Room


def test_room_effective_name_precedence():
    base = dict(robot_id="r", map_id="m", map_version="v", room_id="7")
    assert Room(**base).name == "Room 7"
    assert Room(**base, cloud_name="Laundry Room").name == "Laundry Room"
    assert Room(**base, cloud_name="Laundry Room", local_name="Utility").name == "Utility"
    assert Room(**base, local_name="Utility").model_dump()["name"] == "Utility"


def test_verified_requires_a_cited_run():
    with pytest.raises(ValueError):
        Verification.verified("")
    assert Verification.verified("session b10c0fbc 2026-11-08").is_verified
    assert not Verification.unverified().is_verified


def test_setting_capability_lookup():
    assert Capability.for_setting("pad_wetness") is Capability.SETTING_PAD_WETNESS
    with pytest.raises(ValueError):
        Capability.for_setting("turbo")


def test_every_event_type_has_a_payload_model():
    assert set(EVENT_DATA) == set(EventType)


def test_encode_data_rejects_the_wrong_payload_model():
    assert encode_data(EventType.BIN_FULL, BinFullData()) == {}
    with pytest.raises(TypeError):
        encode_data(EventType.ROBOT_DOCKED, BinFullData())


def test_kit_error_wire_shape_omits_empty_fields():
    assert KitError(ErrorCode.TIMEOUT, "late").to_dict() == {"code": "timeout", "message": "late"}
    err = KitError(ErrorCode.NOT_SUPPORTED, "no", robot_id="r", details={"capability": "evac"})
    assert err.to_dict()["details"] == {"capability": "evac"}
