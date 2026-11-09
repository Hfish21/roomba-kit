from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from roomba_kit.core import registry
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.registry import Operation
from roomba_kit.mcp.server import INTERNAL_MESSAGE, build_mcp, in_process
from roomba_kit.service.describe import MOTION_NOTE, describe
from roomba_kit.service.operations import load_operations
from tests.unit.test_api import I5, LEAKY, register_test_operations


class Ctx:
    version = "test"


@pytest.fixture
def test_ops() -> Iterator[None]:
    load_operations()
    with registry.isolated():
        register_test_operations()
        yield


@pytest.fixture
async def client(test_ops: None) -> AsyncIterator[Client[Any]]:
    async with Client(build_mcp(in_process(Ctx()))) as c:
        yield c


async def test_one_tool_per_mcp_operation(client):
    tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {op.name for op in registry.on("mcp")}
    assert "token_list" not in tools and "boom" in tools
    assert tools["dock"].description == describe(registry.get("dock"))
    assert MOTION_NOTE in tools["dock"].description


async def test_input_schema_is_params_model(client):
    tools = {t.name: t for t in await client.list_tools()}
    schema = tools["robot_state"].input_schema
    assert set(schema["properties"]) == {"robot", "fields", "limit"}
    assert schema["required"] == ["robot"]
    assert tools["health"].input_schema["properties"] == {}


async def test_annotations(client):
    tools = {t.name: t for t in await client.list_tools()}
    assert tools["robot_state"].annotations.read_only_hint is True
    dock = tools["dock"].annotations
    assert dock.destructive_hint is False and dock.idempotent_hint is False
    assert dock.read_only_hint is None
    assert tools["evac"].annotations is None


async def test_call_returns_dumped_result(client):
    result = await client.call_tool("robot_state", {"robot": I5, "fields": ["a"]})
    assert result.structured_content == {"robot": I5, "fields": ["a"], "limit": 10}
    sent = await client.call_tool("dock", {"robot": I5, "wait": False})
    assert sent.is_error is False
    assert sent.structured_content["outcome"] == "unconfirmed"


async def test_non_object_result_is_wrapped(client):
    result = await client.call_tool("rooms_list", {"robot": I5})
    assert result.structured_content == {"result": [{"id": "1", "name": "Kitchen"}]}
    assert result.data[0].name == "Kitchen"


async def test_kit_error_text_starts_with_code(client):
    with pytest.raises(ToolError) as info:
        await client.call_tool("evac", {"robot": I5})
    text = str(info.value)
    assert text.startswith("not_supported: Braava jet m6 does not support evac.")
    assert '"capability": "evac"' in text


async def test_invalid_arguments_are_invalid_request(client):
    result = await client.call_tool("robot_state", {"limit": "lots"}, raise_on_error=False)
    assert result.is_error
    assert result.content[0].text.startswith("invalid_request: Invalid parameters for robot_state")


async def test_unexpected_error_is_generic(client):
    result = await client.call_tool("boom", {}, raise_on_error=False)
    assert result.is_error
    assert result.content[0].text == f"internal: {INTERNAL_MESSAGE}"
    assert LEAKY not in result.content[0].text


async def test_custom_executor_and_op_subset(test_ops):
    calls: list[tuple[str, dict[str, Any]]] = []

    async def remote(op: Operation, arguments: dict[str, Any]) -> Any:
        calls.append((op.name, arguments))
        if op.name == "evac":
            raise KitError(ErrorCode.SERVICE_UNAVAILABLE, "Service not running")
        return {"status": "proxied"}

    ops = [registry.get("health"), registry.get("evac")]
    async with Client(build_mcp(remote, ops)) as c:
        assert {t.name for t in await c.list_tools()} == {"health", "evac"}
        assert (await c.call_tool("health", {})).structured_content == {"status": "proxied"}
        failed = await c.call_tool("evac", {"robot": I5}, raise_on_error=False)
    assert failed.content[0].text == "service_unavailable: Service not running"
    assert calls == [("health", {}), ("evac", {"robot": I5})]
