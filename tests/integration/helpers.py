"""Building blocks for the cross-cutting proof tests (T107–T109).

Also a pytest plugin: `pytest -p tests.integration.helpers` with `ROOMBA_KIT_TEST_CLOUD=fail|hang`
runs any test with the iRobot cloud configured but blocked (T108 reruns the story suites so).
"""

import asyncio
import json
import os
import selectors
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from roomba_kit.api.app import create_app
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import ConfigModel, RobotEntry, save_config
from roomba_kit.core import registry
from roomba_kit.core.errors import ErrorCode
from roomba_kit.core.models import ConnectionState
from roomba_kit.service.app import ServiceContext
from roomba_kit.transport.fake import FakeCloud

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"
FAST_BACKOFF = {"backoff_min": 0.01, "backoff_max": 0.02}


async def until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def configure_robots(i5_password: str | None = "pw-i5", m6_password: str | None = "pw-m6") -> None:
    """config.toml with the i5 ("Vacuum") and m6 ("Mop") fixtures; passwords in credentials."""
    save_config(
        ConfigModel(
            robots=[
                RobotEntry(id=I5, name="Vacuum", address="192.0.2.10", profile="roomba_i5"),
                RobotEntry(id=M6, name="Mop", address="192.0.2.11", profile="braava_m6"),
            ]
        )
    )
    creds = CredentialStore()
    for rid, password in ((I5, i5_password), (M6, m6_password)):
        if password:
            creds.set_robot_password(rid, password)


@asynccontextmanager
async def serving(ctx: ServiceContext) -> AsyncIterator[FastAPI]:
    """The real app with its lifespan in its own task, as `serve` runs it; robots connected."""
    app = create_app(ctx)
    started, stop = asyncio.Event(), asyncio.Event()

    async def lifespan() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(lifespan())
    await until(lambda: started.is_set() or task.done())
    if task.done():
        task.result()
    try:
        await until(
            lambda: all(s.connection is ConnectionState.CONNECTED and s.has_state for s in ctx)
        )
        yield app
    finally:
        stop.set()
        await task


def api_client(app: FastAPI, token: str | None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://kit/v1", headers=headers)


async def http_call(client: httpx.AsyncClient, op: str, params: dict[str, Any]) -> httpx.Response:
    """`op` over HTTP, mapped exactly as the CLI and stdio MCP clients map it."""
    req = registry.http_request(registry.get(op), params)
    return await client.request(req.method, req.path, params=req.query, json=req.body)


# -- clouds that never work ------------------------------------------------------------------


class HangingCloud(FakeCloud):
    """Every call blocks forever, like a black-holed connection."""

    async def _hang(self, call: str) -> Any:
        self.calls.append(call)
        await asyncio.Event().wait()

    async def login(self) -> None:
        await self._hang("login")

    async def robot_password(self, blid: str) -> str | None:
        return await self._hang("robot_password")  # type: ignore[no-any-return]

    async def pmaps(self, blid: str) -> list[dict[str, Any]]:
        return await self._hang("pmaps")  # type: ignore[no-any-return]

    async def mission_history(self, blid: str) -> list[dict[str, Any]]:
        return await self._hang("mission_history")  # type: ignore[no-any-return]


def blocked_cloud(mode: str) -> FakeCloud:
    if mode == "hang":
        return HangingCloud()
    cloud = FakeCloud()
    cloud.fail_everything = True
    return cloud


# -- the cloud-blocked plugin (T108) ---------------------------------------------------------

CLOUD_MODE_ENV = "ROOMBA_KIT_TEST_CLOUD"
REPORT_ENV = "ROOMBA_KIT_TEST_CLOUD_REPORT"


@pytest.fixture(autouse=True)
def _cloud_blocked(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Every ServiceContext gets a configured-but-blocked cloud; records what each one saw."""
    mode = os.environ.get(CLOUD_MODE_ENV)
    if not mode:
        yield
        return
    monkeypatch.setenv("ROOMBA_KIT_CLOUD__USERNAME", "blocked@example.com")
    monkeypatch.setenv("ROOMBA_KIT_CLOUD__PASSWORD", "blocked-cloud-pw")
    seen: list[tuple[ServiceContext, list[FakeCloud]]] = []
    original = ServiceContext.__init__

    def init(self: ServiceContext, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        clouds: list[FakeCloud] = []

        def factory(username: str, password: str, country: str) -> FakeCloud:
            clouds.append(blocked_cloud(mode))
            return clouds[-1]

        self.cloud_factory = factory
        seen.append((self, clouds))

    monkeypatch.setattr(ServiceContext, "__init__", init)
    yield
    row = {
        "test": request.node.nodeid,
        "contexts": [
            {
                "cloud": ctx.cloud.status().model_dump(mode="json"),
                "calls": [c.calls for c in clouds],
            }
            for ctx, clouds in seen
        ],
    }
    with open(os.environ[REPORT_ENV], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def cloud_unavailable_reason(response: httpx.Response) -> str:
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == ErrorCode.CLOUD_UNAVAILABLE.value, error
    return str(error["details"]["reason"])


# -- virtual time (T109, SC-006) -------------------------------------------------------------


class _VirtualSelector:
    """Polls real I/O without blocking; when nothing is ready, jumps the clock to the next timer."""

    def __init__(self, selector: selectors.BaseSelector, loop: "VirtualTimeLoop") -> None:
        self._selector = selector
        self._loop = loop

    def select(self, timeout: float | None = None) -> list[Any]:
        ready = self._selector.select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None:
            raise RuntimeError("virtual time: nothing scheduled and nothing to wait for")
        self._loop.now += timeout
        return []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._selector, name)


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock only moves when every task is waiting on a timer.

    `asyncio.sleep`, `wait_for` and `timeout` all run on `loop.time()`, so code under test keeps
    its real delays (60 s backoffs) and they elapse instantly. Only for code with no threads or
    real I/O: a thread finishing later than the next timer would see time jump past it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.now = 0.0
        self._selector: Any = _VirtualSelector(self._selector, self)

    def time(self) -> float:
        return self.now


def run_virtual[T](main: Callable[[], Awaitable[T]]) -> T:
    with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
        return runner.run(main())  # type: ignore[arg-type]
