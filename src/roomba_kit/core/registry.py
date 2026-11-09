"""The operation registry: the single source of truth for every surface (contracts/operations.md).

Each capability is one async handler registered with `@operation(...)`. The HTTP API, the MCP
server and the CLI are generated from these records, so all three expose the same names,
parameters, results and error codes (Constitution III).
"""

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Literal, Protocol
from urllib.parse import quote

from pydantic import BaseModel, TypeAdapter, ValidationError

from roomba_kit.core.capabilities import Capability
from roomba_kit.core.errors import ErrorCode, KitError

Surface = Literal["http", "mcp", "cli"]
ALL_SURFACES: frozenset[Surface] = frozenset({"http", "mcp", "cli"})
HttpMethod = Literal["GET", "POST", "PUT", "DELETE"]


class OperationContext(Protocol):
    """What the registry hands every handler. `service.app.ServiceContext` implements it.

    Defined here, not in `service`, because `core` must not import `service`. Handlers in
    `service/operations/` may annotate the richer `ServiceContext` they actually receive.
    """

    @property
    def version(self) -> str: ...


Handler = Callable[[Any, Any], Awaitable[Any]]


class NoParams(BaseModel):
    """Params model for operations that take none."""


@dataclass(frozen=True)
class Http:
    """HTTP binding. `path` is relative to `/v1`; `{name}` segments are taken from params."""

    method: HttpMethod
    path: str
    auth: bool = True

    @property
    def path_params(self) -> tuple[str, ...]:
        parts = [seg[1:-1] for seg in self.path.split("/") if seg.startswith("{")]
        return tuple(parts)


@dataclass(frozen=True)
class Cli:
    """CLI binding: `roomba-kit <group…> <name> <args…> [--options]`.

    `args` are params taken positionally, in order; every other param becomes an option.
    `secret` params are never accepted on the command line: they are prompted for with hidden
    input (or read from stdin), so they never land in shell history.
    """

    group: tuple[str, ...]
    name: str
    args: tuple[str, ...] = ()
    secret: tuple[str, ...] = ()


@dataclass(frozen=True)
class Operation:
    name: str
    description: str
    params: type[BaseModel]
    result: Any
    handler: Handler
    capability: Capability | None = None
    read_only: bool = False
    motion: bool = False
    http: Http | None = None
    cli: Cli | None = None
    surfaces: frozenset[Surface] = field(default=ALL_SURFACES)
    local: bool = False
    """Runs inside the CLI process without the service (token admin only)."""

    @cached_property
    def result_adapter(self) -> TypeAdapter[Any]:
        return TypeAdapter(self.result)

    def dump(self, value: Any) -> Any:
        """Serialize a handler result to wire JSON, identically for every surface."""
        return self.result_adapter.dump_python(value, mode="json", by_alias=True)


_REGISTRY: dict[str, Operation] = {}


def operation(
    *,
    name: str | None = None,
    description: str,
    params: type[BaseModel] = NoParams,
    result: Any,
    capability: Capability | None = None,
    read_only: bool = False,
    motion: bool = False,
    http: Http | None = None,
    cli: Cli | None = None,
    surfaces: frozenset[Surface] | set[Surface] = ALL_SURFACES,
    local: bool = False,
) -> Callable[[Handler], Handler]:
    """Register an async handler `(ctx, params) -> result` as a kit operation."""
    surface_set = frozenset(surfaces)

    def decorate(handler: Handler) -> Handler:
        op_name = name or handler.__name__
        if op_name in _REGISTRY:
            raise ValueError(f"operation {op_name!r} is registered twice")
        if "http" in surface_set and http is None:
            raise ValueError(f"operation {op_name!r} is on HTTP but has no Http binding")
        if "cli" in surface_set and cli is None:
            raise ValueError(f"operation {op_name!r} is on the CLI but has no Cli binding")
        if http is not None:
            missing = set(http.path_params) - set(params.model_fields)
            if missing:
                raise ValueError(f"{op_name!r}: path params {sorted(missing)} not in params model")
        _REGISTRY[op_name] = Operation(
            name=op_name,
            description=description,
            params=params,
            result=result,
            handler=handler,
            capability=capability,
            read_only=read_only,
            motion=motion,
            http=http,
            cli=cli,
            surfaces=surface_set,
            local=local,
        )
        return handler

    return decorate


def get(name: str) -> Operation:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KitError(ErrorCode.INVALID_REQUEST, f"Unknown operation: {name}") from None


def all() -> list[Operation]:  # noqa: A001 - registry API named in tasks.md T012
    """Every registered operation, sorted by name."""
    return [_REGISTRY[n] for n in sorted(_REGISTRY)]


def on(surface: Surface) -> list[Operation]:
    return [op for op in all() if surface in op.surfaces]


def parse_params(op: Operation, raw: Mapping[str, Any]) -> BaseModel:
    """Validate raw params; every surface maps a failure to `invalid_request`."""
    try:
        return op.params.model_validate(dict(raw))
    except ValidationError as exc:
        problems = [
            {"param": ".".join(str(p) for p in err["loc"]) or "(body)", "problem": err["msg"]}
            for err in exc.errors(include_url=False, include_input=False)
        ]
        raise KitError(
            ErrorCode.INVALID_REQUEST,
            f"Invalid parameters for {op.name}",
            details={"errors": problems},
        ) from None


async def invoke(op: Operation, ctx: OperationContext, raw: Mapping[str, Any]) -> Any:
    """Validate params, run the handler, return the (unserialized) result."""
    params = parse_params(op, raw)
    return await op.handler(ctx, params)


@contextmanager
def isolated() -> Iterator[None]:
    """Run with an empty registry, restoring the real one afterwards. For tests only."""
    saved = dict(_REGISTRY)
    _REGISTRY.clear()
    try:
        yield
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(saved)


@dataclass(frozen=True)
class HttpRequest:
    method: HttpMethod
    path: str
    query: dict[str, Any]
    body: dict[str, Any] | None


def http_request(op: Operation, params: Mapping[str, Any]) -> HttpRequest:
    """How `params` travel over HTTP for `op` — the one mapping the API and its clients share.

    Path params fill `{name}` segments (URL-quoted); GET/DELETE send the rest as query
    parameters (lists repeat the key); POST/PUT send the rest as a JSON body. `None` values
    are omitted so server-side defaults apply.
    """
    if op.http is None:
        raise ValueError(f"operation {op.name!r} has no HTTP binding")
    path = op.http.path
    rest: dict[str, Any] = {}
    for key, value in params.items():
        if key in op.http.path_params:
            path = path.replace("{" + key + "}", quote(str(value), safe=""))
        elif value is not None:
            rest[key] = value
    if op.http.method in ("GET", "DELETE"):
        return HttpRequest(op.http.method, path, rest, None)
    return HttpRequest(op.http.method, path, {}, rest)
