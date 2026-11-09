"""US4 / FR-047: `roomba-kit … --json` prints exactly the HTTP response body (T064).

Every read operation on both the CLI and HTTP is run both ways against one in-process service,
for a valid robot and an unknown one (so error envelopes are compared too). The CLI runs in a
worker thread; its sync HTTP client is bridged to the app on the test's event loop.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from roomba_kit.cli.client import CliState
from roomba_kit.cli.main import build_cli
from roomba_kit.core import registry
from roomba_kit.core.registry import Operation
from roomba_kit.service.operations import load_operations
from roomba_kit.transport.fake import FakeTransportFactory
from tests.integration._us4 import FIXTURES, Served, make_ctx, sample_params, serving

# CLI commands that a lifecycle extra replaces with a local composite, so their output isn't
# the operation's body by design.
COMPOSITES = {
    "service_status": "`service status` is the lifecycle composite {pid, url, health, "
    "service_status}: it works without the service, so it can't be the HTTP body",
}


def read_ops() -> list[Operation]:
    load_operations()
    return [
        op
        for op in registry.all()
        if op.read_only and not op.local and {"http", "cli"} <= op.surfaces
    ]


class Bridge(httpx.BaseTransport):
    """A sync transport that runs each request on `loop` through the app's ASGI transport."""

    def __init__(self, served: Served, loop: asyncio.AbstractEventLoop) -> None:
        self.served, self.loop = served, loop

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def send() -> httpx.Response:
            forwarded = httpx.Request(
                request.method, request.url, headers=request.headers, content=request.read()
            )
            response = await self.served.transport.handle_async_request(forwarded)
            content = await response.aread()
            return httpx.Response(response.status_code, headers=response.headers, content=content)

        return asyncio.run_coroutine_threadsafe(send(), self.loop).result()


def cli_args(op: Operation, params: dict[str, Any]) -> list[str]:
    assert op.cli is not None
    args = [*op.cli.group, op.cli.name]
    args += [str(params[name]) for name in op.cli.args if name in params]
    for name, value in params.items():
        if name in op.cli.args:
            continue
        flag = "--" + name.replace("_", "-")
        for item in value if isinstance(value, list) else [value]:
            args += [flag, str(item)]
    return args


@pytest.fixture
async def served(tmp_kit_home: Path) -> AsyncIterator[Served]:
    factory = FakeTransportFactory.from_fixtures(FIXTURES, reaction_delay=0.01)
    async with serving(make_ctx(factory)) as s:
        yield s


def test_there_are_read_operations_to_compare() -> None:
    assert len(read_ops()) >= 5, "parity would pass vacuously"


@pytest.mark.parametrize("unknown_robot", [False, True], ids=["robot", "unknown-robot"])
@pytest.mark.parametrize("op", read_ops(), ids=lambda op: op.name)
async def test_cli_json_is_the_http_body(
    op: Operation, unknown_robot: bool, served: Served
) -> None:
    if op.name in COMPOSITES:
        pytest.skip(COMPOSITES[op.name])
    params = sample_params(op)
    if unknown_robot:
        if "robot" not in params:
            pytest.skip("no robot param")
        params["robot"] = "Nobody"
    req = registry.http_request(op, params)
    root = build_cli()
    state = CliState(
        service_url="http://test", transport=Bridge(served, asyncio.get_running_loop())
    )

    async def http() -> bytes:
        resp = await served.client.request(
            req.method, "/v1" + req.path, params=req.query, json=req.body, headers=served.auth
        )
        return resp.content

    def run_cli() -> bytes:
        result = CliRunner().invoke(root, ["--json", *cli_args(op, params)], obj=state)
        assert result.exception is None or isinstance(result.exception, SystemExit), result
        return result.stdout_bytes

    # Fakes keep publishing for a moment after connect (coalesced state events). The CLI must
    # match whenever the body is the same immediately before and after it ran.
    for _ in range(20):
        before = await http()
        printed = await asyncio.to_thread(run_cli)
        if before == await http():
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"{op.name}: the HTTP body never held still")
    assert printed == before, op.name
    if unknown_robot:
        assert b'"robot_not_found"' in printed
