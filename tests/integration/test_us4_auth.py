"""US4 / FR-048 (quickstart V4): non-health endpoints need a token; revocation is live (T063).

Routes are taken from the running app, so endpoints added later are covered automatically.
"""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.routing import APIRoute
from starlette.routing import Route

from roomba_kit.api.app import MCP_PATH
from roomba_kit.cli.main import build_cli
from roomba_kit.core import registry
from roomba_kit.transport.fake import FakeTransportFactory
from tests.integration._us4 import FIXTURES, I5, M6, Served, make_ctx, serving

# FastAPI's own schema and docs pages: no robot data, and the published API description (FR-048).
DOC_PATHS = {"/v1/openapi.json", "/v1/docs", "/docs/oauth2-redirect"}
BAD_HEADERS = [{}, {"Authorization": "Bearer rk_not-a-real-token"}, {"Authorization": "Basic x"}]


@pytest.fixture
def factory() -> FakeTransportFactory:
    return FakeTransportFactory.from_fixtures(FIXTURES, reaction_delay=0.01)


@pytest.fixture
async def served(tmp_kit_home: Path, factory: FakeTransportFactory) -> AsyncIterator[Served]:
    async with serving(make_ctx(factory)) as s:
        yield s


def _protected(served: Served) -> list[tuple[str, str]]:
    """(method, concrete path) for every route that must require a token."""
    found: list[tuple[str, str]] = []
    for route in served.app.routes:
        if not isinstance(route, Route) or route.path in DOC_PATHS:
            continue
        if isinstance(route, APIRoute):
            http = registry.get(route.name).http
            assert http is not None
            if not http.auth:
                continue
        path = re.sub(r"\{[^}]+\}", "Vacuum", route.path)
        methods = (route.methods or {"POST"}) - {"HEAD"}
        found += [(m, path) for m in sorted(methods)]
    return found


async def test_every_non_health_route_rejects_requests_without_a_valid_token(
    served: Served, factory: FakeTransportFactory
) -> None:
    protected = _protected(served)
    assert ("POST", MCP_PATH) in protected
    assert len(protected) >= len(registry.on("http")), "routes went missing"
    for method, path in protected:
        for headers in BAD_HEADERS:
            body = {} if method in ("POST", "PUT") else None
            resp = await served.client.request(method, path, headers=headers, json=body)
            assert resp.status_code == 401, (method, path, headers)
            assert resp.json()["error"]["code"] == "unauthorized", (method, path)
            assert resp.headers["www-authenticate"] == "Bearer"
    # Nothing ran: no robot was sent a command or a setting.
    for robot_id in (I5, M6):
        fake = factory.get(robot_id)
        assert fake.sent_commands == [] and fake.published_settings == []


async def test_health_needs_no_token(served: Served) -> None:
    public = [op for op in registry.on("http") if op.http is not None and not op.http.auth]
    assert {op.name for op in public} <= {"health"}
    for op in public:
        assert op.http is not None
        assert (await served.client.get("/v1" + op.http.path)).status_code == 200


def cli(*args: str) -> str:
    result = CliRunner().invoke(build_cli(), ["--json", *args])
    assert result.exit_code == 0, result.output
    return result.stdout


async def test_a_token_revoked_via_the_cli_stops_working_without_a_restart(served: Served) -> None:
    created = json.loads(await asyncio.to_thread(cli, "token", "create", "consumer"))
    headers = {"Authorization": f"Bearer {created['token']}"}
    assert (await served.client.get("/v1/robots/Vacuum/state", headers=headers)).status_code == 200

    revoked = json.loads(await asyncio.to_thread(cli, "token", "revoke", "consumer"))
    assert revoked == {"name": "consumer", "revoked": True}
    resp = await served.client.get("/v1/robots/Vacuum/state", headers=headers)
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "unauthorized"
    mcp = await served.client.post(MCP_PATH, headers=headers, json={})
    assert mcp.status_code == 401
    # Other tokens are unaffected.
    ok = await served.client.get("/v1/robots/Vacuum/state", headers=served.auth)
    assert ok.status_code == 200
