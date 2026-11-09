from types import SimpleNamespace

import pytest

from roomba_kit.core.capabilities import Capability, Verification
from roomba_kit.profiles import loader
from roomba_kit.profiles.base import ModelProfile, SettingMapping

U = Verification.unverified()


def _profile(pid="test_bot", patterns=(r"^t1",), **kw):
    return ModelProfile(
        id=pid,
        display_name="T",
        sku_patterns=list(patterns),
        capabilities={Capability.CLEAN: U},
        **kw,
    )


def test_profile_id_must_be_snake_case():
    with pytest.raises(ValueError):
        _profile(pid="Test-Bot")


def test_setting_requires_matching_capability():
    mapping = SettingMapping(
        read=lambda r: r.get("childLock"),
        write=lambda v: {"childLock": v},
        group=("childLock",),
        confirm=lambda r, v: r.get("childLock") == v,
    )
    with pytest.raises(ValueError, match="setting.child_lock"):
        _profile(settings={"child_lock": mapping})


def test_match_profile_by_sku(monkeypatch):
    p1, p2 = _profile("alpha", [r"^a1"]), _profile("beta", [r"^b"])
    eps = [SimpleNamespace(name=p.id, load=lambda p=p: p) for p in (p1, p2)]
    broken = SimpleNamespace(name="broken", load=lambda: (_ for _ in ()).throw(ImportError("x")))
    monkeypatch.setattr(loader, "entry_points", lambda group: [*eps, broken])
    loader.load_profiles.cache_clear()
    try:
        assert loader.match_profile("a1G5020") is p1
        assert loader.match_profile("b6") is p2
        assert loader.match_profile("zz") is None
        assert set(loader.load_profiles()) == {"alpha", "beta"}
    finally:
        loader.load_profiles.cache_clear()
