"""The error catalogue in code equals the table in contracts/errors.md (T038)."""

import re
from pathlib import Path

from roomba_kit.core.errors import CLI_EXIT, HTTP_STATUS, ErrorCode

CONTRACT = Path(__file__).parents[2] / "specs/001-roomba-kit-v1/contracts/errors.md"
ROW = re.compile(r"^\| `(?P<code>[a-z_]+)` \|[^|]*\| (?P<http>[^|]+?) \| (?P<exit>\d+) \|$")


def _table() -> dict[str, tuple[int | None, int]]:
    rows: dict[str, tuple[int | None, int]] = {}
    for line in CONTRACT.read_text().splitlines():
        m = ROW.match(line.strip())
        if not m:
            continue
        http = re.match(r"\d{3}", m["http"].strip())
        rows[m["code"]] = (int(http.group()) if http else None, int(m["exit"]))
    return rows


def test_contract_table_parses():
    # Guards against the parser silently matching nothing and passing vacuously.
    assert len(_table()) >= 19


def test_error_codes_equal_contract():
    assert {c.value for c in ErrorCode} == set(_table())


def test_http_status_equals_contract():
    expected = {code: http for code, (http, _) in _table().items() if http is not None}
    assert {c.value: s for c, s in HTTP_STATUS.items()} == expected


def test_cli_exit_equals_contract():
    expected = {code: exit_ for code, (_, exit_) in _table().items()}
    assert {c.value: e for c, e in CLI_EXIT.items()} == expected
