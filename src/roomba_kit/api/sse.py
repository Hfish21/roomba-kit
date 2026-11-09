"""`GET /v1/events` as a Server-Sent Events stream (T053, research R3, contracts/events.md).

With `Accept: text/event-stream`, `events_list` streams instead of returning a page. Each frame
carries `id: <seq>`, `event: <type>` and `data: <envelope JSON>`. A reconnecting client sends
`Last-Event-ID` (or `since`) and gets every event after it, then live ones, with no gap or
repeat. Without either, the stream is live only. `limit` doesn't apply to a stream.
"""

import json
from collections.abc import AsyncIterator

from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel
from sse_starlette import EventSourceResponse, ServerSentEvent

from roomba_kit.api.generate import register_stream_presenter
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.events import EventType
from roomba_kit.service.app import ServiceContext

PING_S = 15


def wants_stream(request: Request) -> bool:
    return "text/event-stream" in request.headers.get("accept", "")


def resume_from(request: Request, since: int | None) -> int | None:
    """`Last-Event-ID` wins over `since`: it's what a reconnecting EventSource sends."""
    raw = request.headers.get("last-event-id")
    if raw is None or not raw.strip():
        return since
    try:
        seq = int(raw)
    except ValueError:
        raise KitError(ErrorCode.INVALID_REQUEST, "Last-Event-ID must be an event seq.") from None
    if seq < 0:
        raise KitError(ErrorCode.INVALID_REQUEST, "Last-Event-ID must be an event seq.")
    return seq


async def present_events(
    request: Request, params: BaseModel, ctx: ServiceContext
) -> Response | None:
    # `params` is `events_list`'s model. It isn't imported here: importing an operations module
    # from the API would register it into whatever registry is active (see `load_operations`).
    if not wants_stream(request):
        return None
    since = resume_from(request, getattr(params, "since", None))
    robot: str | None = getattr(params, "robot", None)
    types: list[EventType] | None = getattr(params, "types", None)
    robot_id = ctx.resolve_robot(robot).robot_id if robot else None

    async def frames() -> AsyncIterator[ServerSentEvent]:
        async for event in ctx.bus.stream(since, robot_id, types):
            envelope = event.model_dump(mode="json", by_alias=True)
            yield ServerSentEvent(
                data=json.dumps(envelope, separators=(",", ":")),
                event=event.type.value,
                id=str(event.seq),
            )

    # Proxies (nginx) buffer event streams unless told not to.
    return EventSourceResponse(frames(), ping=PING_S, headers={"X-Accel-Buffering": "no"})


register_stream_presenter("events_list", present_events)
