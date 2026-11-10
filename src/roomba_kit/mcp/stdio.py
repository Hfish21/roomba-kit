"""`roomba-kit mcp`: the MCP tool set over stdio, proxied to the running service.

The tools are the in-service ones (`build_mcp`), with an executor that sends each call to the
HTTP API using the CLI's token. This process never talks to robots. If the
service is down, every call fails with `service_unavailable`.
"""

from collections.abc import Callable
from typing import Any

import httpx
from fastmcp import FastMCP

from roomba_kit.client import NOT_RUNNING, Client
from roomba_kit.config.tokens import read_cli_token
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.registry import Operation
from roomba_kit.mcp.server import build_mcp

__all__ = ["NOT_RUNNING", "NO_TOKEN", "ServiceExecutor", "build_stdio_mcp", "serve_stdio"]

NO_TOKEN = "No API token found. Create one with: roomba-kit token create cli"  # noqa: S105


class ServiceExecutor:
    """An `Executor` that runs operations by calling the service's HTTP API.

    The token is read on every call, so `token create cli` takes effect without restarting the
    MCP client.
    """

    def __init__(
        self,
        base_url: str,
        token: Callable[[], str | None] = read_cli_token,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self._token = token
        self._transport = transport

    async def __call__(self, op: Operation, arguments: dict[str, Any]) -> Any:
        token = self._token()
        async with Client(self.base_url, token or "", transport=self._transport) as kit:
            try:
                return await kit.request(op, arguments)
            except KitError as err:
                if err.code is ErrorCode.UNAUTHORIZED and not token:
                    err.message = f"{err.message} {NO_TOKEN}"
                raise


def build_stdio_mcp(base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> FastMCP:
    """The `roomba-kit` MCP server, every tool proxied to the service at `base_url`."""
    return build_mcp(ServiceExecutor(base_url, transport=transport))


async def serve_stdio(base_url: str) -> None:
    await build_stdio_mcp(base_url).run_stdio_async(show_banner=False)
