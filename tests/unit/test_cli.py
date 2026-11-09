import json
import stat
import sys
import types

import httpx
import pytest
from click.testing import CliRunner
from pydantic import BaseModel, Field

from roomba_kit.cli import main
from roomba_kit.cli.client import NOT_RUNNING, CliState
from roomba_kit.config import paths
from roomba_kit.core import registry
from roomba_kit.core.errors import CLI_EXIT, ErrorCode
from roomba_kit.core.registry import Cli, Http, operation


class RobotP(BaseModel):
    robot: str


class CleanP(BaseModel):
    robot: str
    rooms: list[str] = Field(default_factory=list)
    ordered: bool = True


class PasswordP(BaseModel):
    robot: str
    password: str


class Out(BaseModel):
    ok: bool = True


@pytest.fixture
def ops():
    with registry.isolated():

        @operation(
            description="Robot state.",
            params=RobotP,
            result=Out,
            read_only=True,
            http=Http("GET", "/robots/{robot}/state"),
            cli=Cli((), "state", args=("robot",)),
        )
        async def get_state(ctx, p):
            return Out()

        @operation(
            description="Clean.",
            params=CleanP,
            result=Out,
            http=Http("POST", "/robots/{robot}/commands/clean"),
            cli=Cli((), "clean", args=("robot",)),
        )
        async def clean(ctx, p):
            return Out()

        @operation(
            description="Store a password.",
            params=PasswordP,
            result=Out,
            http=Http("POST", "/robots/{robot}/password"),
            cli=Cli(("robots",), "password", args=("robot",), secret=("password",)),
        )
        async def set_robot_password(ctx, p):
            return Out()

        yield registry.on("cli")


class Service:
    """An httpx MockTransport standing in for the service; records every request."""

    def __init__(self, status=200, body=b'{"ok":true}', exc=None):
        self.status, self.body, self.exc, self.requests = status, body, exc, []

    def __call__(self, request):
        self.requests.append(request)
        if self.exc:
            raise self.exc
        return httpx.Response(self.status, content=self.body)


def invoke(root, args, service=None, **kwargs):
    obj = CliState(transport=httpx.MockTransport(service or Service()))
    return CliRunner().invoke(root, args, obj=obj, **kwargs)


def test_json_is_byte_identical_to_the_served_body(ops, tmp_kit_home):
    body = '{"robot_id":  "00000000000000000000000000000001", "name":"Küche"}'.encode()
    result = invoke(main.build_cli(ops), ["--json", "state", "kitchen"], Service(body=body))
    assert result.exit_code == 0
    assert result.stdout_bytes == body


def test_request_mapping_and_options(ops, tmp_kit_home):
    service = Service()
    root = main.build_cli(ops)
    args = ["clean", "a/b", "--rooms", "Kitchen", "--rooms", "Hall", "--no-ordered"]
    result = invoke(root, args, service)
    assert result.exit_code == 0, result.output
    (req,) = service.requests
    assert req.method == "POST"
    assert req.url.raw_path == b"/v1/robots/a%2Fb/commands/clean"
    assert json.loads(req.content) == {"rooms": ["Kitchen", "Hall"], "ordered": False}


def test_defaults_are_left_to_the_service(ops, tmp_kit_home):
    service = Service()
    invoke(main.build_cli(ops), ["clean", "r1"], service)
    assert json.loads(service.requests[0].content) == {}


def test_service_url_defaults_to_the_configured_port(ops, tmp_kit_home):
    paths.config_file().write_text("[server]\nport = 9999\n")
    service = Service()
    invoke(main.build_cli(ops), ["state", "r1"], service)
    assert str(service.requests[0].url) == "http://127.0.0.1:9999/v1/robots/r1/state"


def test_token_header_from_env(ops, tmp_kit_home, monkeypatch):
    monkeypatch.setenv("ROOMBA_KIT_API_TOKEN", "rk_test")
    service = Service()
    invoke(main.build_cli(ops), ["state", "r1"], service)
    assert service.requests[0].headers["authorization"] == "Bearer rk_test"


def test_refused_connection_exits_69(ops, tmp_kit_home):
    service = Service(exc=httpx.ConnectError("refused"))
    result = invoke(main.build_cli(ops), ["state", "r1"], service)
    assert result.exit_code == 69
    assert NOT_RUNNING in result.stderr


@pytest.mark.parametrize(
    "code", [ErrorCode.ROBOT_NOT_FOUND, ErrorCode.NOT_SUPPORTED, ErrorCode.ROBOT_OFFLINE]
)
def test_error_envelope_maps_to_exit_code(ops, tmp_kit_home, code):
    body = json.dumps({"error": {"code": code.value, "message": "nope"}}).encode()
    result = invoke(main.build_cli(ops), ["state", "r1"], Service(status=400, body=body))
    assert result.exit_code == CLI_EXIT[code]
    assert f"Error ({code.value}): nope" in result.stderr


def test_unauthorized_without_token_hints(ops, tmp_kit_home):
    body = b'{"error":{"code":"unauthorized","message":"missing token"}}'
    result = invoke(main.build_cli(ops), ["state", "r1"], Service(status=401, body=body))
    assert result.exit_code == 77
    assert "token create cli" in result.stderr


def test_unconfirmed_exits_3_and_still_prints(ops, tmp_kit_home):
    body = b'{"outcome":"unconfirmed","state":{"activity":"cleaning"}}'
    result = invoke(main.build_cli(ops), ["--json", "clean", "r1"], Service(status=202, body=body))
    assert result.exit_code == 3
    assert result.stdout_bytes == body


def test_quiet_suppresses_command_output_only(ops, tmp_kit_home):
    root = main.build_cli(ops)
    assert invoke(root, ["-q", "clean", "r1"]).stdout == ""
    assert "ok" in invoke(root, ["-q", "state", "r1"]).stdout


def test_secret_params_are_never_options(ops, tmp_kit_home):
    service = Service()
    root = main.build_cli(ops)
    rejected = invoke(root, ["robots", "password", "r1", "--password", "pw"], service)
    assert rejected.exit_code == 2 and "No such option" in rejected.stderr
    assert service.requests == []
    result = invoke(root, ["robots", "password", "r1"], service, input="s3cret\n")
    assert result.exit_code == 0, result.output
    assert json.loads(service.requests[0].content) == {"password": "s3cret"}
    assert "s3cret" not in result.output


def test_register_seam_replaces_a_generated_command(ops, tmp_kit_home, monkeypatch):
    import click

    extra = types.ModuleType("roomba_kit.cli._extra")

    def register(root):
        @click.command("clean")
        def alias():
            click.echo("alias")

        root.add_command(alias)

    extra.register = register
    monkeypatch.setitem(sys.modules, extra.__name__, extra)
    monkeypatch.setattr(main, "extension_modules", lambda: [extra.__name__])
    result = invoke(main.build_cli(ops), ["clean"])
    assert result.stdout == "alias\n"


def test_token_ops_run_locally_without_the_service(tmp_kit_home):
    service = Service(exc=httpx.ConnectError("refused"))
    created = invoke(main.cli, ["--json", "token", "create", "cli"], service)
    assert created.exit_code == 0, created.output
    token = json.loads(created.stdout)["token"]
    assert paths.cli_token_file().read_text().strip() == token
    assert stat.S_IMODE(paths.cli_token_file().stat().st_mode) == 0o600

    listed = invoke(main.cli, ["--json", "token", "list"], service)
    (row,) = json.loads(listed.stdout)
    assert set(row) == {"name", "created_at", "last_used_at"} and row["name"] == "cli"
    assert token not in listed.stdout

    dup = invoke(main.cli, ["token", "create", "cli"], service)
    assert dup.exit_code == 2 and "already exists" in dup.stderr

    revoked = invoke(main.cli, ["--json", "token", "revoke", "cli"], service)
    assert json.loads(revoked.stdout) == {"name": "cli", "revoked": True}
    assert not paths.cli_token_file().exists()
    assert service.requests == []
