import dataclasses
from datetime import UTC, datetime

from roomba_kit.profiles.roomba_i5 import PROFILE as I5
from roomba_kit.profiles.texts import ERROR_TEXTS, NOT_READY_TEXTS
from roomba_kit.service.normalize import code_text, normalize

AT = datetime(2026, 11, 8, 12, tzinfo=UTC)
I5_ID = "0" * 31 + "1"


def _codes(error, not_ready):
    status = {"phase": "charge", "cycle": "none", "error": error, "notReady": not_ready}
    return {"batPct": 100, "cleanMissionStatus": status}


def test_tables_cover_the_codes_seen_on_hardware():
    assert NOT_READY_TEXTS[68] == "Not ready yet (transient around docking and bin emptying)"
    assert "transient" in NOT_READY_TEXTS[23] and "transient" in NOT_READY_TEXTS[39]
    assert ERROR_TEXTS[17] == "Path blocked" and ERROR_TEXTS[18] == "Docking issue"
    assert all(text and text.strip() == text for text in [*ERROR_TEXTS.values()])
    assert not any(
        "@val" in t or "%s" in t for t in [*ERROR_TEXTS.values(), *NOT_READY_TEXTS.values()]
    )


def test_lookup_order_profile_then_shared_then_fallback():
    assert code_text(18, {18: "Dock blocked by a chair"}, ERROR_TEXTS, "Error") == (
        "Dock blocked by a chair"
    )
    assert code_text(18, {}, ERROR_TEXTS, "Error") == "Docking issue"
    assert code_text(9999, {}, ERROR_TEXTS, "Error") == "Error 9999"


def test_normalize_uses_profile_overrides_first():
    profile = dataclasses.replace(I5, not_ready_texts={68: "Lifted off the Clean Base"})
    s = normalize(I5_ID, _codes(18, 68), profile, AT)
    assert (s.error.code, s.error.text) == (18, "Docking issue")
    assert (s.not_ready.code, s.not_ready.text) == (68, "Lifted off the Clean Base")
    s = normalize(I5_ID, _codes(0, 9999), I5, AT)
    assert s.error is None and s.not_ready.text == "Not ready 9999"
