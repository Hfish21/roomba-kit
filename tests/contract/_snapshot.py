"""Snapshot files for the contract tests. Regenerate after an intended contract change:

    UPDATE_SNAPSHOTS=1 uv run pytest tests/contract

then review the diff: it is exactly what consumers of the API, MCP tools or CLI will see change.
A breaking change also bumps the contract version.
"""

import json
import os
from pathlib import Path
from typing import Any

SNAPSHOTS = Path(__file__).parent / "snapshots"
UPDATE = os.environ.get("UPDATE_SNAPSHOTS") == "1"


def as_json(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def check(name: str, text: str) -> None:
    path = SNAPSHOTS / name
    if UPDATE:
        path.write_text(text, encoding="utf-8")
        return
    assert path.exists(), f"no snapshot {name}; create it with UPDATE_SNAPSHOTS=1"
    assert text == path.read_text(encoding="utf-8"), (
        f"{name} changed. If that's intended, run UPDATE_SNAPSHOTS=1 uv run pytest tests/contract "
        "and review the diff."
    )
