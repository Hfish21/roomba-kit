"""Events and mission waits (T052, FR-036, FR-037, contracts/events.md).

`GET /v1/events` also streams SSE with `Accept: text/event-stream` (api/sse.py); that is a
presentation of `events_list`, not another operation.
"""

import asyncio
from typing import Literal

from pydantic import BaseModel, Field

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import Event, EventType
from roomba_kit.core.models import TERMINAL_OUTCOMES, KitModel, Mission
from roomba_kit.core.registry import Cli, Http, operation
from roomba_kit.service import derive
from roomba_kit.service.app import ServiceContext

WAIT_MAX_S = 300
MISSION_WAIT_MAX_S = 6 * 3600


class EventsListParams(BaseModel):
    since: int | None = Field(
        None, ge=0, description="Return events with a seq greater than this; omit for the oldest"
    )
    robot: str | None = Field(None, description="Only this robot (ID or unambiguous name)")
    types: list[EventType] | None = Field(None, description="Only these event types")
    limit: int = Field(100, ge=1, le=1000, description="Most events to return")


class EventsWaitParams(BaseModel):
    since: int = Field(ge=0, description="Wait for events with a seq greater than this")
    robot: str | None = Field(None, description="Only this robot (ID or unambiguous name)")
    types: list[EventType] | None = Field(None, description="Only these event types")
    timeout_s: float = Field(
        30, gt=0, le=WAIT_MAX_S, description="Seconds to wait before returning no events"
    )


class EventPage(KitModel):
    events: list[Event]
    next: int = Field(description="Pass as `since` to continue after these events")


class MissionWaitParams(BaseModel):
    robot: str = Field(description="Robot ID or unambiguous name")
    mission_id: str | None = Field(
        None, description="The mission to wait for; omit for the current or next mission"
    )
    timeout_s: float = Field(
        1800, gt=0, le=MISSION_WAIT_MAX_S, description="Seconds to wait for the mission to end"
    )


class MissionWaitTimeout(KitModel):
    timeout: Literal[True] = True
    mission: Mission | None = Field(description="The mission still in progress, if any")


def _robot_id(ctx: ServiceContext, ref: str | None) -> str | None:
    return ctx.resolve_robot(ref).robot_id if ref is not None else None


@operation(
    description=(
        "List recorded events, oldest first, after `since`. Over HTTP, send "
        "`Accept: text/event-stream` to get the same events as a live SSE stream that resumes "
        "with `Last-Event-ID`."
    ),
    params=EventsListParams,
    result=EventPage,
    read_only=True,
    http=Http("GET", "/events"),
    cli=Cli(("events",), "list"),
)
async def events_list(ctx: ServiceContext, params: EventsListParams) -> EventPage:
    robot_id = _robot_id(ctx, params.robot)
    events, next_ = await ctx.bus.read(params.since or 0, robot_id, params.types, params.limit)
    return EventPage(events=events, next=next_)


@operation(
    description=(
        "Wait for events after `since` and return them as soon as any arrive, or an empty "
        "list at the timeout. Call again with `next` to keep following."
    ),
    params=EventsWaitParams,
    result=EventPage,
    read_only=True,
    http=Http("GET", "/events/wait"),
    cli=Cli(("events",), "wait"),
)
async def events_wait(ctx: ServiceContext, params: EventsWaitParams) -> EventPage:
    robot_id = _robot_id(ctx, params.robot)
    events = await ctx.bus.wait(params.since, robot_id, params.types, params.timeout_s)
    return EventPage(events=events, next=events[-1].seq if events else params.since)


@operation(
    description=(
        "Wait until a robot's mission ends and return it with its outcome. Without "
        "`mission_id` it waits for the current mission, or the next one if none is running. "
        "Returns `{timeout: true, mission}` if it hasn't ended in time."
    ),
    params=MissionWaitParams,
    result=Mission | MissionWaitTimeout,
    read_only=True,
    http=Http("GET", "/robots/{robot}/missions/wait"),
    cli=Cli(("missions",), "wait", args=("robot",)),
)
async def mission_wait(
    ctx: ServiceContext, params: MissionWaitParams
) -> Mission | MissionWaitTimeout:
    robot_id = ctx.resolve_robot(params.robot).robot_id
    tracker = derive.deriver(ctx)
    if tracker is None:
        raise KitError(ErrorCode.INTERNAL, "Mission tracking isn't running.", robot_id)
    since = ctx.bus.last_seq  # before looking, so a mission ending meanwhile isn't missed
    target = params.mission_id
    if target is not None:
        known = tracker.find(robot_id, target)
        if known is None:
            raise KitError(
                ErrorCode.INVALID_REQUEST,
                f"No current or recent mission {target!r} on this robot.",
                robot_id,
                {"current": (c.id if (c := tracker.current(robot_id)) else None)},
            )
        if known.outcome in TERMINAL_OUTCOMES:
            return known
    else:
        current = tracker.current(robot_id)
        target = current.id if current else None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + params.timeout_s
    while (remaining := deadline - loop.time()) > 0:
        events = await ctx.bus.wait(since, robot_id, [EventType.MISSION_FINISHED], remaining)
        for event in events:
            since = event.seq
            mission = Mission.model_validate(event.data["mission"])
            if target is None or mission.id == target:
                return mission
    return MissionWaitTimeout(mission=tracker.current(robot_id))
