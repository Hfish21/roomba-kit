"""The MCP server: one tool per registry operation on the `mcp` surface (contracts/surfaces.md).

Tools are built against an `Executor`, so the same tool set serves the in-service HTTP mount
(which runs handlers in process) and the stdio proxy (which forwards to the HTTP API).
"""

import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from fastmcp import FastMCP
from fastmcp.tools import Tool, ToolResult
from mcp.types import TextContent, ToolAnnotations
from pydantic import PrivateAttr

from roomba_kit.core import registry
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.registry import Operation, OperationContext
from roomba_kit.service.describe import describe

log = logging.getLogger(__name__)

SERVER_NAME = "roomba-kit"
INTERNAL_MESSAGE = "Unexpected failure; see the service log."

Executor = Callable[[Operation, dict[str, Any]], Awaitable[Any]]
"""Runs `op` with raw arguments and returns its wire JSON (`op.dump`), or raises `KitError`."""


def in_process(ctx: OperationContext) -> Executor:
    """An executor that runs the handler in this process against `ctx`."""

    async def execute(op: Operation, arguments: dict[str, Any]) -> Any:
        return op.dump(await registry.invoke(op, ctx, arguments))

    return execute


def error_text(err: KitError) -> str:
    """`<code>: <message>`, then the error object as JSON when it carries a robot or details."""
    text = f"{err.code.value}: {err.message}"
    if err.robot_id is not None or err.details is not None:
        text += "\n" + json.dumps({"error": err.to_dict()})
    return text


def _output_schema(op: Operation) -> dict[str, Any]:
    schema = op.result_adapter.json_schema(mode="serialization")
    if schema.get("type") == "object":
        return schema
    defs = schema.pop("$defs", None)
    wrapped: dict[str, Any] = {
        "type": "object",
        "properties": {"result": schema},
        "required": ["result"],
        "x-fastmcp-wrap-result": True,
    }
    if defs:
        wrapped["$defs"] = defs
    return wrapped


def _annotations(op: Operation) -> ToolAnnotations | None:
    if op.read_only:
        return ToolAnnotations(read_only_hint=True)
    if op.motion:
        return ToolAnnotations(destructive_hint=False, idempotent_hint=False)
    return None


class OperationTool(Tool):
    """A registry operation as an MCP tool. Arguments are validated by the registry, not MCP."""

    _op: Operation = PrivateAttr()
    _executor: Executor = PrivateAttr()

    @classmethod
    def build(cls, op: Operation, executor: Executor) -> "OperationTool":
        tool = cls(
            name=op.name,
            description=describe(op),
            parameters=op.params.model_json_schema(),
            output_schema=_output_schema(op),
            annotations=_annotations(op),
        )
        tool._op = op
        tool._executor = executor
        return tool

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            wire = await self._executor(self._op, arguments)
        except KitError as err:
            return _error(error_text(err))
        except Exception:
            log.exception("MCP tool %s failed", self.name)
            return _error(f"{ErrorCode.INTERNAL.value}: {INTERNAL_MESSAGE}")
        return self.convert_result(wire)


def _error(text: str) -> ToolResult:
    return ToolResult(content=[TextContent(type="text", text=text)], is_error=True)


def build_mcp(executor: Executor, ops: Iterable[Operation] | None = None) -> FastMCP:
    """`FastMCP("roomba-kit")` with one tool per operation (default: every `mcp` operation)."""
    server: FastMCP = FastMCP(SERVER_NAME)
    for op in registry.on("mcp") if ops is None else ops:
        server.add_tool(OperationTool.build(op, executor))
    return server
