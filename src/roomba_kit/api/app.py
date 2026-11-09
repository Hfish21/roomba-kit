"""The HTTP API: a FastAPI app generated from the registry, with the MCP server at `/mcp`."""

import importlib
import json
import pkgutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

import roomba_kit.api
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
    for info in pkgutil.iter_modules(roomba_kit.api.__path__):  # e.g. api/sse.py self-registers
        importlib.import_module(f"roomba_kit.api.{info.name}")
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
        # Served below behind bearer auth: FR-048 exempts only the health check.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.ctx = context
    app.add_exception_handler(HTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    _add_routes(app, registry.on("http"))
    app.router.routes.append(Route(MCP_PATH, BearerGuard(mcp_app, context)))
    _add_doc_routes(app, context)
    return app


_SWAGGER = """<!doctype html>
<html><head><meta charset="utf-8"><title>Roomba Kit API</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head><body><div id="ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({spec: __SPEC__, dom_id: "#ui"});</script>
</body></html>"""


def _deny(request: Request, ctx: ServiceContext) -> Response | None:
    try:
        authenticate(request.headers.raw, ctx)
    except KitError as err:
        return error_response(err)
    return None


def _add_doc_routes(app: FastAPI, ctx: ServiceContext) -> None:
    """OpenAPI JSON and Swagger UI, both behind bearer auth (FR-048). The UI embeds the spec
    inline, so it works once the page itself was fetched with a token."""

    async def openapi_json(request: Request) -> Response:
        return _deny(request, ctx) or JSONResponse(app.openapi())

    async def docs(request: Request) -> Response:
        if (denied := _deny(request, ctx)) is not None:
            return denied
        spec = json.dumps(app.openapi()).replace("</", "<\\/")
        return HTMLResponse(_SWAGGER.replace("__SPEC__", spec))

    app.router.routes.append(Route(f"{API_PREFIX}/openapi.json", openapi_json, methods=["GET"]))
    app.router.routes.append(Route(f"{API_PREFIX}/docs", docs, methods=["GET"]))
