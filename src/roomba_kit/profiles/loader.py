"""Discover model profiles from the `roomba_kit.profiles` entry-point group."""

import logging
from functools import cache
from importlib.metadata import entry_points

from roomba_kit.profiles.base import ModelProfile

ENTRY_POINT_GROUP = "roomba_kit.profiles"
log = logging.getLogger(__name__)


@cache
def load_profiles() -> dict[str, ModelProfile]:
    """Every installed profile, keyed by profile ID. Cached; call `.cache_clear()` to reload.

    A profile that fails to import is logged and skipped, so one broken third-party profile
    can't take the service down.
    """
    profiles: dict[str, ModelProfile] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            profile = ep.load()
        except Exception:
            log.exception("failed to load model profile %s", ep.name)
            continue
        if not isinstance(profile, ModelProfile):
            log.error("entry point %s is not a ModelProfile; skipped", ep.name)
            continue
        if profile.id in profiles:
            log.error("duplicate model profile id %s; keeping the first", profile.id)
            continue
        profiles[profile.id] = profile
    return profiles


def get_profile(profile_id: str) -> ModelProfile | None:
    return load_profiles().get(profile_id)


def match_profile(sku: str) -> ModelProfile | None:
    """The profile whose `sku_patterns` match the discovery SKU, or None (unsupported)."""
    for profile in sorted(load_profiles().values(), key=lambda p: p.id):
        if profile.matches(sku):
            return profile
    return None
