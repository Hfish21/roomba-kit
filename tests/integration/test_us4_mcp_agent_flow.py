"""US4 / SC-008 (quickstart V4): an agent with only the MCP tools cleans a room and reports (T066).

The flow an agent follows for "clean the laundry room with the vacuum and tell me when it's
done": `list_robots` → `clean_rooms` → `mission_wait`. It runs through both MCP transports'
tool sets: the in-service one (`/mcp`) and the stdio proxy, which forwards over HTTP.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from roomba_kit.core.models import Activity
from roomba_kit.mcp.server import build_mcp, in_process
from roomba_kit.mcp.stdio import ServiceExecutor
from roomba_kit.service import cloud
from roomba_kit.transport.fake import FakeCloud, FakeTransportFactory
from tests.integration._us4 import FIXTURES, I5, M6, Served, make_ctx, serving, until


@pytest.fixture
def factory() -> FakeTransportFactory:
    return FakeTransportFactory.from_fixtures(FIXTURES, reaction_delay=0.01)


@pytest.fixture
async def served(
    tmp_kit_home: Path, factory: FakeTransportFactory, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Served]:
    monkeypatch.setattr(cloud, "POLL_S", 0.02)
    ctx = make_ctx(factory, cloud=FakeCloud.from_fixtures(FIXTURES))
    async with serving(ctx) as s:
        await until(lambda: ctx.cloud.status().status == "connected")
        # The owner imported room names once, during setup.
        imported = await s.client.post(f"/v1/robots/{I5}/rooms/import", headers=s.auth)
        assert imported.status_code == 200, imported.text
        yield s


@pytest.fixture(params=["in-service", "stdio-proxy"])
async def agent(request: pytest.FixtureRequest, served: Served) -> AsyncIterator[Client[Any]]:
    if request.param == "in-service":
        executor = in_process(served.app.state.ctx)
    else:
        executor = ServiceExecutor("http://test", transport=served.transport)
    async with Client(build_mcp(executor)) as client:
        yield client


async def finish_on_its_own(factory: FakeTransportFactory, served: Served) -> None:
    """Play the robot: once it's cleaning, it heads home and docks."""
    session = served.app.state.ctx.sessions[I5]
    await until(lambda: session.state.activity is Activity.CLEANING)
    fake = factory.get(I5)
    status = dict(fake.reported["cleanMissionStatus"])
    fake.emit({"cleanMissionStatus": {**status, "phase": "hmPostMsn"}})
    await asyncio.sleep(0.05)
    fake.emit({"cleanMissionStatus": {**status, "cycle": "none", "phase": "charge"}})


async def test_clean_a_room_and_report_when_done(
    agent: Client[Any], served: Served, factory: FakeTransportFactory
) -> None:
    tools = {t.name: t for t in await agent.list_tools()}
    assert {"list_robots", "clean_rooms", "mission_wait"} <= set(tools)
    assert "Supported on:" in (tools["clean_rooms"].description or "")

    robots = (await agent.call_tool("list_robots", {})).structured_content["result"]
    vacuum = next(r for r in robots if r["name"] == "Vacuum")

    started = await agent.call_tool(
        "clean_rooms", {"robot": vacuum["id"], "rooms": ["Laundry Room"]}
    )
    assert started.structured_content["outcome"] == "confirmed"

    robot = asyncio.create_task(finish_on_its_own(factory, served))
    done = await agent.call_tool("mission_wait", {"robot": vacuum["id"], "timeout_s": 5})
    await robot
    mission = done.structured_content["result"]  # a union result is wrapped
    assert "timeout" not in mission, mission
    assert mission["robot_id"] == I5 and mission["rooms_requested"] == ["7"]
    assert mission["outcome"] == "completed" and mission["ended_at"]


async def test_evac_on_the_mop_is_not_supported(
    agent: Client[Any], factory: FakeTransportFactory
) -> None:
    with pytest.raises(ToolError) as info:
        await agent.call_tool("evac", {"robot": "Mop"})
    assert str(info.value).startswith("not_supported")
    assert M6 in str(info.value)
    assert factory.get(M6).sent_commands == []  # the robot was not contacted
