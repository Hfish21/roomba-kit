"""The HTTP API: a FastAPI app generated from the registry, with the MCP server at `/mcp`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from roomba_kit.api.generate import (
    API_PREFIX,
    Presenter,
    authenticate,
    error_response,
    register_stream_presenter,
    stream_presenters,
)
from roomba_kit.api.generate import add_routes as _add_routes
from roomba_kit.core import registry
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.mcp.server import build_mcp, in_process
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.operations import load_operations

__all__ = ["MCP_PATH", "Presenter", "create_app", "register_stream_presenter", "stream_presenters"]

MCP_PATH = "/mcp"


class BearerGuard:
    """ASGI wrapper: rejects HTTP requests without a valid bearer token before delegating."""

    def __init__(self, app: ASGIApp, ctx: ServiceContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            try:
                authenticate(scope["headers"], self.ctx)
            except KitError as err:
                await error_response(err)(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    """Unknown paths and methods keep their status but still answer in the kit envelope."""
    status = exc.status_code if isinstance(exc, HTTPException) else 400
    message = f"No such endpoint: {request.method} {request.url.path}"
    response = error_response(KitError(ErrorCode.INVALID_REQUEST, message))
    response.status_code = status
    return response


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    return error_response(KitError(ErrorCode.INVALID_REQUEST, "Invalid request."))


def create_app(ctx: ServiceContext | None = None) -> FastAPI:
    """The service's ASGI app. Its lifespan starts and stops `ctx` (built here if not given)."""
    load_operations()
    context = ctx if ctx is not None else ServiceContext()
    mcp_app = build_mcp(in_process(context)).http_app(transport="http", path=MCP_PATH)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await context.start()
        try:
            async with mcp_app.lifespan(mcp_app):
                yield
        finally:
            await context.stop()

    app = FastAPI(
        title="Roomba Kit",
        version=context.version,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.ctx = context
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    _add_routes(app, registry.on("http"))
    app.router.routes.append(Route(MCP_PATH, BearerGuard(mcp_app, context)))
    return app
