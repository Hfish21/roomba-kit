"""Every registry operation is identical on HTTP, MCP and CLI.

Each operation must appear on every surface it declares, under its own name, with exactly the
params of its params model, and with a result schema derived from the same model. The only ops
allowed off a surface are the exceptions recorded in plan.md Complexity Tracking.
"""

from typing import Any

import click
import pytest
from fastmcp import Client

from roomba_kit.api.app import create_app
from roomba_kit.cli.generate import add_operations
from roomba_kit.cli.main import build_cli
from roomba_kit.core import registry
from roomba_kit.core.registry import Operation
from roomba_kit.mcp.server import build_mcp
from roomba_kit.service.operations import load_operations

ALL = frozenset({"http", "mcp", "cli"})
# plan.md Complexity Tracking: secrets in requests never go through an LLM; token admin is local.
EXCEPTIONS: dict[str, frozenset[str]] = {
    "set_robot_password": frozenset({"http", "cli"}),
    "cloud_login": frozenset({"http", "cli"}),
    "token_create": frozenset({"cli"}),
    "token_list": frozenset({"cli"}),
    "token_revoke": frozenset({"cli"}),
}


def _ops() -> list[Operation]:
    load_operations()
    return registry.all()


def _result_keys(schema: dict[str, Any], defs: dict[str, Any]) -> set[str]:
    """Top-level property names of a result schema, following one `$ref` and array items."""
    if "$ref" in schema:
        schema = defs[schema["$ref"].rsplit("/", 1)[-1]]
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        return {"[]", *_result_keys(schema["items"], defs)}
    return set(schema.get("properties", {}))


def _expected_result_keys(op: Operation) -> set[str]:
    schema = op.result_adapter.json_schema(mode="serialization")
    return _result_keys(schema, schema.get("$defs", {}))


def test_registry_is_not_empty():
    assert _ops(), "no operations registered; parity would pass vacuously"


@pytest.mark.parametrize("op", _ops(), ids=lambda op: op.name)
def test_surfaces_are_all_three_unless_an_allowed_exception(op):
    assert op.surfaces == EXCEPTIONS.get(op.name, ALL)


def test_http_matches_registry():
    doc = create_app().openapi()
    by_id = {
        meta["operationId"]: (path, method, meta)
        for path, methods in doc["paths"].items()
        for method, meta in methods.items()
    }
    defs = doc.get("components", {}).get("schemas", {})
    for op in _ops():
        if "http" not in op.surfaces:
            assert op.name not in by_id, f"{op.name} must not be on HTTP"
            continue
        path, method, meta = by_id[op.name]
        assert (method.upper(), path) == (op.http.method, "/v1" + op.http.path)
        names = {p["name"] for p in meta.get("parameters", [])}
        body = meta.get("requestBody", {}).get("content", {}).get("application/json", {})
        names |= set(body.get("schema", {}).get("properties", {}))
        assert names == set(op.params.model_fields), op.name
        result = meta["responses"]["200"]["content"]["application/json"]["schema"]
        assert _result_keys(result, defs) == _expected_result_keys(op), op.name


async def test_mcp_matches_registry():
    ops = _ops()

    async def never(op, params):  # tools are listed, not called
        raise AssertionError

    async with Client(build_mcp(never)) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for op in ops:
        if "mcp" not in op.surfaces:
            assert op.name not in tools, f"{op.name} must not be on MCP"
            continue
        tool = tools[op.name]
        assert set(tool.input_schema.get("properties", {})) == set(op.params.model_fields), op.name
        out = tool.output_schema or {}
        keys = _result_keys(out, out.get("$defs", {}))
        if out.get("x-fastmcp-wrap-result"):
            res = out["properties"]["result"]
            keys = _result_keys(res, out.get("$defs", {}))
        assert keys == _expected_result_keys(op), op.name
    assert set(tools) == {op.name for op in ops if "mcp" in op.surfaces}


def _find(root: click.Group, path: tuple[str, ...]) -> click.Command | None:
    node: click.Command | None = root
    for part in path:
        if not isinstance(node, click.Group):
            return None
        node = node.commands.get(part)
    return node


def test_cli_matches_registry():
    ops = _ops()
    generated = click.Group("roomba-kit")
    add_operations(generated, ops)
    final = build_cli()
    for op in ops:
        if "cli" not in op.surfaces:
            assert op.cli is None or _find(generated, (*op.cli.group, op.cli.name)) is None
            continue
        path = (*op.cli.group, op.cli.name)
        cmd = _find(generated, path)
        assert cmd is not None, f"{op.name} missing from the generated CLI"
        names = {p.name for p in cmd.params} | set(op.cli.secret)
        assert names == set(op.params.model_fields), op.name
        # Presentation extras (aliases, watchers) may replace the command but never remove it.
        assert _find(final, path) is not None, f"{op.name} missing from the final CLI tree"


def _contract_ops() -> set[str]:
    """Every operation name in the first column of the tables in contracts/operations.md."""
    import re
    from pathlib import Path

    text = (Path(__file__).parents[2] / "docs/contracts/operations.md").read_text()
    names: set[str] = set()
    for line in text.splitlines():
        if m := re.match(r"^\| ((?:`[a-z_]+`(?: / )?)+) \|", line):
            names |= set(re.findall(r"`([a-z_]+)`", m.group(1)))
    return names


def test_registry_is_exactly_the_contract():
    contract = _contract_ops()
    assert len(contract) >= 40, "parser matched too little; the check would pass vacuously"
    registered = {op.name for op in _ops()}
    assert registered - contract == set(), "registered but not in contracts/operations.md"
    assert contract - registered == set(), "in contracts/operations.md but not registered"


def _leaves(group: click.Group, path: tuple[str, ...] = ()) -> list[str]:
    out: list[str] = []
    for name, cmd in group.commands.items():
        if isinstance(cmd, click.Group):
            out += _leaves(cmd, (*path, name))
        else:
            out.append(" ".join((*path, name)))
    return out


def test_every_cli_command_is_in_the_surfaces_contract():
    from pathlib import Path

    text = (Path(__file__).parents[2] / "docs/contracts/surfaces.md").read_text()
    tree = text[text.index("- Command tree:") :]
    leaves = _leaves(build_cli())
    assert len(leaves) >= 40, "too few commands; the check would pass vacuously"
    assert [c for c in leaves if f"`{c}" not in tree] == []
