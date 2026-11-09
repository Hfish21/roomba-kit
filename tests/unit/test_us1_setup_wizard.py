"""`roomba-kit setup` (T046) against a mocked service: it only calls registry operations."""

import json

import httpx
import pytest
from click.testing import CliRunner

from roomba_kit.cli import main, setup_wizard
from roomba_kit.cli.client import CliState
from roomba_kit.config import paths
from roomba_kit.core import registry
from roomba_kit.service.operations import load_operations

I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"
VACUUM = {
    "id": I5,
    "name": "Vacuum",
    "sku": "i5G5020",
    "firmware": "fw",
    "address": "192.0.2.10",
    "supported": True,
    "profile_id": "roomba_i5",
    "configured": False,
}
MOP = {**VACUUM, "id": M6, "name": "Mop", "sku": "m611020", "address": "192.0.2.11"}
MOP["profile_id"] = "braava_m6"


class Service:
    """Answers each operation from `answers`: op name -> body, or a list of bodies in turn
    (so a list-valued body is wrapped: `list_robots=[[]]`)."""

    def __init__(self, **answers):
        load_operations()
        self.answers = answers
        self.calls: list[tuple[str, dict]] = []
        self.routes = {
            (op.http.method, "/v1" + op.http.path): op
            for op in registry.all()
            if op.http is not None
        }

    def _match(self, method: str, path: str):
        """The operation for a request, and its path params."""
        for (m, pattern), op in self.routes.items():
            parts, want = path.split("/"), pattern.split("/")
            if m == method and len(parts) == len(want):
                pairs = list(zip(want, parts, strict=True))
                if all(w.startswith("{") or w == p for w, p in pairs):
                    return op, {w[1:-1]: p for w, p in pairs if w.startswith("{")}
        raise AssertionError(f"no operation for {method} {path}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        op, params = self._match(request.method, request.url.path)
        params |= dict(request.url.params)
        if request.content:
            params |= json.loads(request.content)
        self.calls.append((op.name, params))
        answer = self.answers.get(op.name, {})
        if isinstance(answer, list):
            answer = answer.pop(0)
        status = 400 if isinstance(answer, dict) and "error" in answer else 200
        return httpx.Response(status, json=answer)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture(autouse=True)
def service_up(monkeypatch):
    monkeypatch.setattr(setup_wizard.daemon, "health", lambda url: {"status": "ok"})


def run(service: Service, keys: list[str]):
    obj = CliState(transport=httpx.MockTransport(service))
    return CliRunner().invoke(main.build_cli(), ["setup"], obj=obj, input="\n".join(keys) + "\n")


def test_first_run(tmp_kit_home):
    service = Service(
        list_robots=[[]],
        discover_robots=[[VACUUM, MOP]],
        add_robot=[{**VACUUM, "configured": True}, {**MOP, "configured": True}],
        retrieve_password=[
            {"outcome": "stored", "steps": []},
            {"outcome": "cloud_only", "steps": []},
        ],
        cloud_status={"status": "not_configured"},
        cloud_login={"status": "connected"},
        cloud_retrieve_password={"outcome": "stored"},
        import_rooms={"imported": 4},
    )
    keys = [
        "y",  # add Vacuum
        "y",  # add Mop
        "",  # Vacuum on dock
        "",  # Mop on dock
        "s",  # skip Mop after cloud_only
        "y",  # add account
        "me@example.com",
        "account-secret",
        "",  # country US
        "y",  # Mop's password from the account
    ]
    result = run(service, keys)
    assert result.exit_code == 0, result.output
    expected = [
        "list_robots",
        "discover_robots",
        "add_robot",
        "add_robot",
        "retrieve_password",
        "retrieve_password",
        "cloud_status",
        "cloud_login",
        "cloud_retrieve_password",
    ]
    if any(op.name == "import_rooms" for op in registry.all()):  # US3's op, once it lands
        expected += ["import_rooms", "import_rooms"]
    assert service.names() == expected
    assert dict(service.calls)["cloud_login"] == {
        "username": "me@example.com",
        "password": "account-secret",
        "country": "US",
    }
    assert dict(service.calls)["cloud_retrieve_password"] == {"robot": M6}
    assert "hold HOME" in result.output and "works without them" in result.output
    assert "account-secret" not in result.output
    assert paths.cli_token_file().exists()  # first run minted the cli token


def test_empty_discovery_explains_vpn_and_adds_by_ip(tmp_kit_home):
    service = Service(
        list_robots=[[]],
        discover_robots=[[], []],
        add_robot={**VACUUM, "configured": True},
        retrieve_password={"outcome": "stored", "steps": []},
        cloud_status={"status": "not_configured"},
    )
    keys = ["192.0.2.10", "", "n"]  # the robot's IP, it's on its dock, no account
    result = run(service, keys)
    assert result.exit_code == 0, result.output
    assert "VPN" in result.output and "192.0.2.255" in result.output
    assert service.calls[1:4] == [
        ("discover_robots", {}),
        ("discover_robots", {"broadcast": "192.0.2.10"}),
        ("add_robot", {"address": "192.0.2.10"}),
    ]
    assert "cloud_login" not in service.names()


def test_retry_then_manual_password(tmp_kit_home):
    service = Service(
        list_robots=[[{**VACUUM, "connection": "not_configured"}]] * 3,
        retrieve_password={"outcome": "timeout", "steps": []},
        set_robot_password={"outcome": "stored"},
    )
    keys = ["2", "1", "", "m", "robot-secret", "6"]  # re-pair, pick Vacuum, manual entry, done
    result = run(service, keys)
    assert result.exit_code == 0, result.output
    assert service.names() == [
        "list_robots",
        "list_robots",
        "retrieve_password",
        "set_robot_password",
        "list_robots",
    ]
    assert ("set_robot_password", {"robot": I5, "password": "robot-secret"}) in service.calls
    assert "robot-secret" not in result.output


def test_menu_remove_touches_only_that_robot(tmp_kit_home):
    robots = [{**VACUUM, "connection": "connected"}, {**MOP, "connection": "connected"}]
    service = Service(list_robots=[robots, robots, [robots[0]]], remove_robot={"removed": True})
    result = run(service, ["5", "2", "y", "6"])
    assert result.exit_code == 0, result.output
    assert service.names() == ["list_robots", "list_robots", "remove_robot", "list_robots"]
    assert service.calls[2] == ("remove_robot", {"robot": M6})


def test_declining_to_start_the_service_exits_69(tmp_kit_home, monkeypatch):
    monkeypatch.setattr(setup_wizard.daemon, "health", lambda url: None)
    service = Service()
    result = run(service, ["n"])
    assert result.exit_code == 69 and service.calls == []


def test_robots_password_manual_alias(tmp_kit_home):
    service = Service(set_robot_password={"outcome": "stored"})
    obj = CliState(transport=httpx.MockTransport(service))
    args = ["robots", "password", "Vacuum", "--manual"]
    result = CliRunner().invoke(main.build_cli(), args, obj=obj, input="pw\n")
    assert result.exit_code == 0, result.output
    assert service.calls == [("set_robot_password", {"robot": "Vacuum", "password": "pw"})]
