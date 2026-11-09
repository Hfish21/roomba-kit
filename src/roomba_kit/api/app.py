"""The HTTP API: a FastAPI app generated from the registry."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from roomba_kit.api.generate import (
    API_PREFIX,
    Presenter,
    error_response,
    register_stream_presenter,
    stream_presenters,
)
from roomba_kit.api.generate import add_routes as _add_routes
from roomba_kit.core import registry
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.operations import load_operations

__all__ = ["Presenter", "create_app", "register_stream_presenter", "stream_presenters"]


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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await context.start()
        try:
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
    return app
