"""HTTP routes and their OpenAPI, generated from the operation registry (contracts/surfaces.md).

Each route accepts exactly what `registry.http_request` produces: path params from the path,
the rest as query params (GET/DELETE, lists as repeated keys) or a JSON object body (POST/PUT).
Validation is the registry's own, so a bad request is always the kit's `invalid_request`
envelope and never FastAPI's 422.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, TypeAdapter
from pydantic.json_schema import JsonSchemaMode

from roomba_kit.core import registry
from roomba_kit.core.contract import API_PREFIX
from roomba_kit.core.errors import HTTP_STATUS, ErrorCode, KitError
from roomba_kit.core.models import CommandResult
from roomba_kit.core.registry import Http, Operation
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.describe import describe

log = logging.getLogger(__name__)

BEARER = "bearer"
INTERNAL_MESSAGE = "Unexpected failure; see the service log."

Presenter = Callable[[Request, BaseModel, ServiceContext], Awaitable[Response | None]]

stream_presenters: dict[str, Presenter] = {}
"""Alternative presentations of an operation's result, keyed by operation name.

A presenter runs after auth and param validation, before the handler. It returns a Response
(e.g. SSE when the client sends `Accept: text/event-stream`) or None to fall through to JSON.
"""


def register_stream_presenter(op_name: str, presenter: Presenter) -> None:
    stream_presenters[op_name] = presenter


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    robot_id: str | None = None
    details: dict[str, Any] | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorBody


def error_response(err: KitError) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if err.code is ErrorCode.UNAUTHORIZED else None
    return JSONResponse(
        {"error": err.to_dict()}, status_code=HTTP_STATUS[err.code], headers=headers
    )


def authenticate(headers: Iterable[tuple[bytes, bytes]], ctx: ServiceContext) -> str:
    """The token's name, or raises `unauthorized`. Takes raw ASGI headers so MCP can share it."""
    value = next((v.decode("latin-1") for k, v in headers if k.lower() == b"authorization"), "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        name = ctx.tokens.verify(token.strip())
        if name is not None:
            return name
    raise KitError(ErrorCode.UNAUTHORIZED, "Missing or invalid API token.")


def _is_array(schema: dict[str, Any]) -> bool:
    return schema.get("type") == "array" or any(_is_array(s) for s in schema.get("anyOf", []))


async def _raw_params(http: Http, request: Request, arrays: frozenset[str]) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if http.method in ("GET", "DELETE"):
        query = request.query_params
        for key in dict.fromkeys(query.keys()):
            raw[key] = query.getlist(key) if key in arrays else query[key]
    else:
        body = await request.body()
        if body.strip():
            try:
                data = json.loads(body)
            except ValueError:
                raise KitError(
                    ErrorCode.INVALID_REQUEST, "Request body is not valid JSON."
                ) from None
            if not isinstance(data, dict):
                raise KitError(ErrorCode.INVALID_REQUEST, "Request body must be a JSON object.")
            raw.update(data)
    raw.update(request.path_params)
    return raw


def _endpoint(op: Operation, http: Http) -> Callable[[Request], Awaitable[Response]]:
    props = op.params.model_json_schema().get("properties", {})
    arrays = frozenset(name for name, schema in props.items() if _is_array(schema))

    async def endpoint(request: Request) -> Response:
        ctx: ServiceContext = request.app.state.ctx
        try:
            if http.auth:
                authenticate(request.scope["headers"], ctx)
            params = registry.parse_params(op, await _raw_params(http, request, arrays))
            if (presenter := stream_presenters.get(op.name)) is not None:
                if (response := await presenter(request, params, ctx)) is not None:
                    return response
            result = await op.handler(ctx, params)
            body = op.dump(result)
        except KitError as err:
            return error_response(err)
        except Exception:
            log.exception("operation %s failed", op.name)
            return error_response(KitError(ErrorCode.INTERNAL, INTERNAL_MESSAGE))
        unconfirmed = isinstance(result, CommandResult) and result.outcome == "unconfirmed"
        return JSONResponse(body, status_code=202 if unconfirmed else 200)

    return endpoint


# -- OpenAPI ----------------------------------------------------------------------------------


def _schemas(ops: list[Operation]) -> tuple[dict[tuple[str, str], Any], dict[str, Any]]:
    """Every params/result schema, with shared `$defs` hoisted into `components.schemas`."""
    inputs: list[tuple[tuple[str, str], JsonSchemaMode, TypeAdapter[Any]]] = [
        (("error", ""), "serialization", TypeAdapter(ErrorEnvelope))
    ]
    for op in ops:
        inputs.append((("params", op.name), "validation", TypeAdapter(op.params)))
        inputs.append((("result", op.name), "serialization", op.result_adapter))
    by_key, defs = TypeAdapter.json_schemas(inputs, ref_template="#/components/schemas/{model}")
    return {key: schema for (key, _mode), schema in by_key.items()}, defs.get("$defs", {})


def _resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref", "")
    resolved: dict[str, Any] = defs[ref.rsplit("/", 1)[-1]] if ref else schema
    return resolved


def _openapi_extra(
    op: Operation, http: Http, schemas: dict[tuple[str, str], Any], defs: dict[str, Any]
) -> dict[str, Any]:
    params_schema = _resolve(schemas[("params", op.name)], defs)
    props: dict[str, Any] = params_schema.get("properties", {})
    required = set(params_schema.get("required", []))
    path_params = set(http.path_params)
    extra: dict[str, Any] = {}

    def param(name: str, where: str) -> dict[str, Any]:
        schema = props[name]
        entry = {"name": name, "in": where, "required": where == "path" or name in required}
        if "description" in schema:
            entry["description"] = schema["description"]
        return {**entry, "schema": schema}

    parameters = [param(n, "path") for n in http.path_params if n in props]
    rest = {n: s for n, s in props.items() if n not in path_params}
    if http.method in ("GET", "DELETE"):
        parameters += [param(n, "query") for n in rest]
    elif rest:
        body: dict[str, Any] = {"type": "object", "properties": rest}
        if body_required := [n for n in rest if n in required]:
            body["required"] = body_required
        extra["requestBody"] = {
            "required": bool(body_required),
            "content": {"application/json": {"schema": body}},
        }
    if parameters:
        extra["parameters"] = parameters

    def content(schema: Any) -> dict[str, Any]:
        return {"application/json": {"schema": schema}}

    responses: dict[str, Any] = {
        "200": {"description": "Success", "content": content(schemas[("result", op.name)])},
        "default": {"description": "Error", "content": content(schemas[("error", "")])},
    }
    if op.result is CommandResult:
        responses["202"] = {
            "description": "Sent, but the state change wasn't observed (outcome=unconfirmed)",
            "content": content(schemas[("result", op.name)]),
        }
    extra["responses"] = responses
    if http.auth:
        extra["security"] = [{BEARER: []}]
    return extra


def add_routes(app: FastAPI, ops: list[Operation]) -> None:
    """One route per HTTP operation, and an OpenAPI document describing their real schemas."""
    bound = [(op, op.http) for op in ops if op.http is not None]
    schemas, defs = _schemas([op for op, _ in bound])
    for op, http in bound:
        app.add_api_route(
            API_PREFIX + http.path,
            _endpoint(op, http),
            methods=[http.method],
            name=op.name,
            operation_id=op.name,
            summary=op.description.strip().splitlines()[0] if op.description.strip() else op.name,
            description=describe(op),
            response_class=JSONResponse,
            openapi_extra=_openapi_extra(op, http, schemas, defs),
        )

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            doc = get_openapi(
                title=app.title, version=app.version, routes=app.routes, servers=app.servers
            )
            components = doc.setdefault("components", {})
            components.setdefault("schemas", {}).update(defs)
            components["securitySchemes"] = {BEARER: {"type": "http", "scheme": "bearer"}}
            app.openapi_schema = doc
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
