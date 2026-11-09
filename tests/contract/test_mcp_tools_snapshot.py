"""MCP tool names, descriptions, schemas and annotations are a contract (FR-049): diffs show it."""

from typing import Any

from fastmcp import Client

from roomba_kit.mcp.server import build_mcp
from roomba_kit.service.operations import load_operations
from tests.contract._snapshot import as_json, check


async def _never(*_: Any) -> Any:
    raise AssertionError("listing tools must not run them")


async def test_mcp_tools_snapshot():
    load_operations()
    async with Client(build_mcp(_never)) as client:
        tools = await client.list_tools()
    listed = [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
            "output_schema": t.output_schema,
            "annotations": t.annotations.model_dump(exclude_none=True) if t.annotations else None,
        }
        for t in sorted(tools, key=lambda t: t.name)
    ]
    assert listed, "no tools"
    check("mcp_tools.json", as_json(listed))
