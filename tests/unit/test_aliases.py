"""`roomba-kit clean` dispatches to `clean` or `clean_rooms` (cli/aliases.py)."""

import json

import pytest
from pydantic import BaseModel

from roomba_kit.cli import main
from roomba_kit.core import registry
from roomba_kit.core.errors import CLI_EXIT, ErrorCode
from roomba_kit.core.registry import Cli, Http, operation
from tests.unit.test_cli import Service, invoke

UNCONFIRMED = b'{"robot_id":"r","command":"clean_rooms","outcome":"unconfirmed"}'


class RobotP(BaseModel):
    robot: str


class RoomsP(BaseModel):
    robot: str
    rooms: list[str]
    ordered: bool = True
    room_options: str | None = None


class Out(BaseModel):
    ok: bool = True


def _clean_rooms() -> None:
    @operation(
        description="Clean rooms.",
        params=RoomsP,
        result=Out,
        motion=True,
        http=Http("POST", "/robots/{robot}/commands/clean_rooms"),
        cli=Cli(("rooms",), "clean", args=("robot",)),
    )
    async def clean_rooms(ctx, p):
        return Out()


@pytest.fixture
def root():
    with registry.isolated():

        @operation(
            description="Clean the whole home.",
            params=RobotP,
            result=Out,
            motion=True,
            http=Http("POST", "/robots/{robot}/commands/clean"),
            cli=Cli((), "clean", args=("robot",)),
        )
        async def clean(ctx, p):
            return Out()

        _clean_rooms()
        yield main.build_cli(registry.on("cli"))


def body(service: Service) -> tuple[str, dict]:
    (req,) = service.requests
    return req.url.path, json.loads(req.content)


def test_no_room_is_a_whole_home_clean(root, tmp_kit_home):
    service = Service()
    assert invoke(root, ["clean", "Vacuum"], service).exit_code == 0
    assert body(service) == ("/v1/robots/Vacuum/commands/clean", {})


def test_rooms_go_to_clean_rooms_in_order(root, tmp_kit_home):
    service = Service()
    args = ["clean", "Vacuum", "--room", "Laundry Room", "--room", "4", "--unordered"]
    assert invoke(root, args, service).exit_code == 0
    assert body(service) == (
        "/v1/robots/Vacuum/commands/clean_rooms",
        {"rooms": ["Laundry Room", "4"], "ordered": False},
    )


def test_exit_codes_and_json_match_the_generated_commands(root, tmp_kit_home):
    error = b'{"error":{"code":"room_not_found","message":"No room","details":{"valid":[]}}}'
    result = invoke(root, ["--json", "clean", "V", "--room", "Nowhere"], Service(404, error))
    assert result.exit_code == CLI_EXIT[ErrorCode.ROOM_NOT_FOUND]
    assert result.stdout_bytes == error
    unconfirmed = invoke(root, ["clean", "V", "--room", "Den"], Service(202, UNCONFIRMED))
    assert unconfirmed.exit_code == CLI_EXIT[ErrorCode.UNCONFIRMED]


def test_unordered_without_rooms_is_refused(root, tmp_kit_home):
    service = Service()
    result = invoke(root, ["clean", "V", "--unordered"], service)
    assert result.exit_code == CLI_EXIT[ErrorCode.INVALID_REQUEST]
    assert service.requests == []


def test_the_generated_room_clean_stays_reachable(root, tmp_kit_home):
    service = Service()
    args = ["rooms", "clean", "V", "--rooms", "Den", "--rooms", "Office"]
    assert invoke(root, args, service).exit_code == 0
    assert body(service)[1] == {"rooms": ["Den", "Office"]}


def test_without_a_clean_op_rooms_are_still_cleanable(tmp_kit_home):
    with registry.isolated():
        _clean_rooms()
        root = main.build_cli(registry.on("cli"))
    service = Service()
    assert invoke(root, ["clean", "V"], service).exit_code == CLI_EXIT[ErrorCode.INVALID_REQUEST]
    assert invoke(root, ["clean", "V", "--room", "Den"], service).exit_code == 0


def test_without_clean_rooms_the_generated_clean_is_kept(tmp_kit_home):
    with registry.isolated():

        @operation(
            description="Clean.",
            params=RobotP,
            result=Out,
            http=Http("POST", "/robots/{robot}/commands/clean"),
            cli=Cli((), "clean", args=("robot",)),
        )
        async def clean(ctx, p):
            return Out()

        root = main.build_cli(registry.on("cli"))
    assert [p.name for p in root.commands["clean"].params] == ["robot"]
