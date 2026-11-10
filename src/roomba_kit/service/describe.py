"""Human/LLM-facing descriptions of operations, shared by MCP tools and the generated docs."""

from typing import Any

from roomba_kit.core.capabilities import Capability
from roomba_kit.core.registry import Operation
from roomba_kit.profiles.loader import load_profiles

MOTION_NOTE = "⚠ Moves the robot: it will physically start, stop or drive."
PER_SETTING_OPS = frozenset({"get_settings", "set_setting"})
"""Operations with no single capability: each `setting.<name>` has its own."""


def support(capability: Capability) -> dict[str, str]:
    """`{profile_id: "verified" | "unverified"}` for every profile declaring `capability`."""
    return {
        pid: profile.capabilities[capability].status
        for pid, profile in sorted(load_profiles().items())
        if capability in profile.capabilities
    }


def setting_support() -> str:
    """Each known setting with the installed models that support it and their verification."""
    lines = []
    for cap in Capability:
        if cap.value.startswith("setting."):
            sup = support(cap)
            where = ", ".join(f"{p} ({s})" for p, s in sup.items()) or "no installed model"
            lines.append(f"- `{cap.value.removeprefix('setting.')}`: {where}")
    return "Settings, the models that support each, and verification:\n" + "\n".join(lines)


def type_name(annotation: Any) -> str:
    """A stable, readable type name. Plain classes by name; generics and unions via `str()`.

    (Don't use `__name__` for unions: on Python 3.14 it is `"Union"`, so docs would differ
    between interpreter versions.)
    """
    if isinstance(annotation, type) and not getattr(annotation, "__args__", None):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def param_docs(op: Operation) -> list[str]:
    lines = []
    for name, field in op.params.model_fields.items():
        kind = type_name(field.annotation)
        default = "" if field.is_required() else f" (default: {field.get_default()!r})"
        text = f" — {field.description}" if field.description else ""
        lines.append(f"- `{name}`: {kind}{default}{text}")
    return lines


def describe(op: Operation) -> str:
    """Purpose, parameters, supported models, verification and the motion warning."""
    parts = [op.description.strip()]
    params = param_docs(op)
    if params:
        parts.append("Parameters:\n" + "\n".join(params))
    if op.capability is not None:
        sup = support(op.capability)
        parts.append("Supported on: " + (", ".join(sup) if sup else "no installed model"))
        if sup:
            parts.append("Verification: " + ", ".join(f"{p}: {s}" for p, s in sup.items()))
    elif op.name in PER_SETTING_OPS:
        parts.append(setting_support())
    if op.motion:
        parts.append(MOTION_NOTE)
    return "\n\n".join(parts)
