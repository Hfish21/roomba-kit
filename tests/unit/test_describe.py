from pydantic import BaseModel, Field

from roomba_kit.core import registry
from roomba_kit.core.capabilities import Capability
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service.describe import MOTION_NOTE, describe


class P(BaseModel):
    robot: str = Field(description="Robot ID or name")


def test_description_has_support_verification_and_motion():
    with registry.isolated():

        @operation(
            description="Empty the bin.",
            params=P,
            result=P,
            capability=Capability.EVAC,
            motion=True,
            http=Http("POST", "/robots/{robot}/commands/evac"),
            cli=Cli((), "evac", args=("robot",)),
        )
        async def evac(ctx, params):
            return params

        text = describe(registry.get("evac"))
    assert text.startswith("Empty the bin.")
    assert "`robot`" in text and "Robot ID or name" in text
    assert "Supported on: roomba_i5" in text and "braava_m6" not in text
    assert "Verification: roomba_i5: verified" in text
    assert MOTION_NOTE in text


def test_type_names_are_stable_across_python_versions():
    from roomba_kit.service.describe import type_name

    assert type_name(str) == "str"
    assert type_name(bool | int | str) == "bool | int | str"
    assert type_name(list[str]) == "list[str]"
    assert type_name(int | None) == "int | None"


def test_settings_ops_list_per_setting_support_and_verification():
    """FR-049: `get_settings`/`set_setting` have no single capability, so they list each
    setting's models and verification, generated from the installed profiles."""
    from roomba_kit.profiles.loader import load_profiles
    from roomba_kit.service.operations import load_operations

    load_operations()
    profiles = load_profiles()
    for name in ("get_settings", "set_setting"):
        text = describe(registry.get(name))
        for cap in (c for c in Capability if c.value.startswith("setting.")):
            declared = [
                f"{pid} ({p.capabilities[cap].status})"
                for pid, p in sorted(profiles.items())
                if cap in p.capabilities
            ]
            where = ", ".join(declared) or "no installed model"
            assert f"- `{cap.value.removeprefix('setting.')}`: {where}" in text, (name, text)
    assert "`pad_wetness`: braava_m6 (" in text and "`carpet_boost`: no installed model" in text
