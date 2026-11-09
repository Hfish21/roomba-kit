import pytest
from pydantic import BaseModel

from roomba_kit.core import registry
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import ActivityTransition
from roomba_kit.core.registry import Cli, Http, operation


class RobotParams(BaseModel):
    robot: str
    limit: int = 10


class Ctx:
    version = "test"


@pytest.fixture(autouse=True)
def _isolated_registry():
    with registry.isolated():
        yield


def _register(**overrides):
    kwargs = dict(
        description="Echo",
        params=RobotParams,
        result=RobotParams,
        http=Http("GET", "/robots/{robot}/echo"),
        cli=Cli(("robots",), "echo", args=("robot",)),
    )
    kwargs.update(overrides)

    @operation(**kwargs)
    async def echo(ctx, params):
        return params

    return echo


def test_registers_under_function_name_with_all_surfaces():
    _register()
    op = registry.get("echo")
    assert op.surfaces == {"http", "mcp", "cli"}
    assert op.http.path_params == ("robot",)
    assert [o.name for o in registry.all()] == ["echo"]


def test_duplicate_name_is_rejected():
    _register()
    with pytest.raises(ValueError, match="twice"):
        _register()


def test_surface_without_binding_is_rejected():
    with pytest.raises(ValueError, match="no Http binding"):
        _register(http=None)
    with pytest.raises(ValueError, match="no Cli binding"):
        _register(name="other", cli=None)


def test_path_param_must_exist_in_params():
    with pytest.raises(ValueError, match="path params"):
        _register(http=Http("GET", "/robots/{nope}"))


def test_cli_only_op_needs_no_http_binding():
    _register(http=None, surfaces={"cli"}, local=True)
    assert registry.on("http") == []
    assert [o.name for o in registry.on("cli")] == ["echo"]


async def test_invoke_validates_params_as_invalid_request():
    _register()
    op = registry.get("echo")
    assert (await registry.invoke(op, Ctx(), {"robot": "r1"})).limit == 10
    with pytest.raises(KitError) as exc:
        await registry.invoke(op, Ctx(), {"limit": "many"})
    assert exc.value.code is ErrorCode.INVALID_REQUEST
    params = {e["param"] for e in exc.value.details["errors"]}
    assert params == {"robot", "limit"}


def test_unknown_operation_is_invalid_request():
    with pytest.raises(KitError) as exc:
        registry.get("nope")
    assert exc.value.code is ErrorCode.INVALID_REQUEST


def test_dump_uses_wire_aliases():
    _register(result=ActivityTransition)
    op = registry.get("echo")
    value = ActivityTransition.model_validate(
        {"robot_id": "r", "at": "2026-11-08T00:00:00Z", "from": "docked", "to": "cleaning"}
    )
    dumped = op.dump(value)
    assert dumped["from"] == "docked" and "from_" not in dumped
