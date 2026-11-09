"""`roomba-kit mcp`: the MCP tool set over stdio, proxied to the running service (research R2).

The tools are the in-service ones (`build_mcp`), with an executor that sends each call to the
HTTP API using the CLI's token. This process never talks to robots (Constitution II). If the
service is down, every call fails with `service_unavailable`.
"""

from collections.abc import Callable
from typing import Any

import httpx
from fastmcp import FastMCP

from roomba_kit.config.tokens import read_cli_token
from roomba_kit.core import registry
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.registry import Operation
from roomba_kit.mcp.server import build_mcp

NOT_RUNNING = "The Roomba Kit service isn't running. Start it with: roomba-kit service start"
NO_TOKEN = "No API token found. Create one with: roomba-kit token create cli"  # noqa: S105


def _error_from(body: Any) -> KitError | None:
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return None
    try:
        code = ErrorCode(str(error.get("code")))
    except ValueError:
        code = ErrorCode.INTERNAL
    robot_id = error.get("robot_id")
    details = error.get("details")
    return KitError(
        code,
        str(error.get("message", "")),
        robot_id=robot_id if isinstance(robot_id, str) else None,
        details=details if isinstance(details, dict) else None,
    )


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
        self.base_url = base_url.rstrip("/") + "/v1"
        self._token = token
        self._transport = transport

    async def __call__(self, op: Operation, arguments: dict[str, Any]) -> Any:
        registry.parse_params(op, arguments)  # same `invalid_request` as the service, sooner
        req = registry.http_request(op, arguments)
        token = self._token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # No read timeout: wait operations hold the request open for their own `timeout_s`.
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            transport=self._transport,
            timeout=httpx.Timeout(5.0, read=None),
        ) as client:
            try:
                response = await client.request(
                    req.method, req.path, params=req.query, json=req.body
                )
            except (httpx.ConnectError, httpx.ConnectTimeout):
                raise KitError(ErrorCode.SERVICE_UNAVAILABLE, NOT_RUNNING) from None
            except httpx.TransportError:
                raise KitError(
                    ErrorCode.SERVICE_UNAVAILABLE, "Lost the connection to the service."
                ) from None
        try:
            body = response.json()
        except ValueError:
            body = None
        if (err := _error_from(body)) is not None:
            if err.code is ErrorCode.UNAUTHORIZED and not token:
                err.message = f"{err.message} {NO_TOKEN}"
            raise err
        if response.is_error or body is None:
            message = f"Unexpected response from the service: HTTP {response.status_code}"
            raise KitError(ErrorCode.INTERNAL, message)
        return body


def build_stdio_mcp(base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> FastMCP:
    """The `roomba-kit` MCP server, every tool proxied to the service at `base_url`."""
    return build_mcp(ServiceExecutor(base_url, transport=transport))


async def serve_stdio(base_url: str) -> None:
    await build_stdio_mcp(base_url).run_stdio_async(show_banner=False)
