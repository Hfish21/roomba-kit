"""Human/LLM-facing descriptions of operations, shared by MCP tools and the generated docs."""

from roomba_kit.core.capabilities import Capability
from roomba_kit.core.registry import Operation
from roomba_kit.profiles.loader import load_profiles

MOTION_NOTE = "⚠ Moves the robot: it will physically start, stop or drive."


def support(capability: Capability) -> dict[str, str]:
    """`{profile_id: "verified" | "unverified"}` for every profile declaring `capability`."""
    return {
        pid: profile.capabilities[capability].status
        for pid, profile in sorted(load_profiles().items())
        if capability in profile.capabilities
    }


def param_docs(op: Operation) -> list[str]:
    lines = []
    for name, field in op.params.model_fields.items():
        kind = getattr(field.annotation, "__name__", None) or str(field.annotation)
        default = "" if field.is_required() else f" (default: {field.get_default()!r})"
        text = f" — {field.description}" if field.description else ""
        lines.append(f"- `{name}`: {kind}{default}{text}")
    return lines


def describe(op: Operation) -> str:
    """Purpose, parameters, supported models, verification and the motion warning (FR-049)."""
    parts = [op.description.strip()]
    params = param_docs(op)
    if params:
        parts.append("Parameters:\n" + "\n".join(params))
    if op.capability is not None:
        sup = support(op.capability)
        parts.append("Supported on: " + (", ".join(sup) if sup else "no installed model"))
        if sup:
            parts.append("Verification: " + ", ".join(f"{p}: {s}" for p, s in sup.items()))
    if op.motion:
        parts.append(MOTION_NOTE)
    return "\n\n".join(parts)
