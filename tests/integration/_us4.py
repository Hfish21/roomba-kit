"""Shared US4 harness: a two-robot service on fakes, served in process, and sample params.

No network: robots, discovery, probing and the cloud are all fakes.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from roomba_kit.api.app import create_app
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import ConfigModel, RobotEntry, load_settings, save_config
from roomba_kit.core.models import ConnectionState, DiscoveredRobot
from roomba_kit.core.registry import Operation
from roomba_kit.service.app import ServiceContext
from roomba_kit.transport.fake import FakeCloud, FakeTransportFactory

FIXTURES = Path(__file__).parents[1] / "fixtures"
I5 = "00000000000000000000000000000001"
M6 = "00000000000000000000000000000002"

# A value for every required param name an operation may have. A new op whose required param
# isn't here fails the US4 tests loudly instead of going unchecked.
SAMPLES: dict[str, Any] = {
    "robot": "Vacuum",
    "since": 10**9,  # beyond any seq, so waits time out deterministically
    "mission_id": "00000000-0000-0000-0000-000000000000",
    "setting": "passes",
    "value": "two",
    "rooms": ["Kitchen"],
    "room_id": "7",
    "name": "Kitchen",
    "address": "192.0.2.50",
    "username": "user@example.com",
    "password": "not-a-real-password",
}
FAST = {"timeout_s": 0.05}
"""Optional params overridden so wait operations return promptly."""


def sample_params(op: Operation) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for name, field in op.params.model_fields.items():
        if name in FAST:
            params[name] = FAST[name]
        elif field.is_required():
            assert name in SAMPLES, f"{op.name}: add a sample for required param {name!r}"
            params[name] = SAMPLES[name]
    return params


async def until(predicate: Any, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def _no_robots(*_: Any) -> list[DiscoveredRobot]:
    return []


async def _nobody(*_: Any) -> DiscoveredRobot | None:
    return None


def make_ctx(factory: FakeTransportFactory) -> ServiceContext:
    """Call under `tmp_kit_home`: writes a two-robot config and their passwords."""
    save_config(
        ConfigModel(
            robots=[
                RobotEntry(id=I5, name="Vacuum", address="192.0.2.10", profile="roomba_i5"),
                RobotEntry(id=M6, name="Mop", address="192.0.2.11", profile="braava_m6"),
            ]
        )
    )
    creds = CredentialStore()
    creds.set_robot_password(I5, "pw1")
    creds.set_robot_password(M6, "pw2")
    return ServiceContext(
        settings=load_settings(),
        transport_factory=factory,
        cloud_factory=lambda *_: FakeCloud.from_fixtures(FIXTURES),
        discover=_no_robots,
        probe=_nobody,
        session_options={"backoff_min": 0.01, "backoff_max": 0.02},
    )


@dataclass
class Served:
    app: FastAPI
    transport: httpx.ASGITransport
    client: httpx.AsyncClient
    """Unauthenticated; base URL `http://test`."""
    token: str
    """The `cli` token, also written where the CLI and stdio MCP read it."""

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@asynccontextmanager
async def serving(ctx: ServiceContext) -> AsyncIterator[Served]:
    """The app with its lifespan in its own task (as a server runs it), robots connected."""
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
    token = ctx.tokens.create("cli")
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await until(lambda: all(s.connection is ConnectionState.CONNECTED for s in ctx))
            await until(lambda: all(s.has_state for s in ctx))
            yield Served(app, transport, client, token)
    finally:
        stop.set()
        await task
