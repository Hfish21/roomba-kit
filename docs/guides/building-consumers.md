# Building consumers

Automations, bots and apps talk to the running service, never to the robots: the service owns
each robot's single local connection. You have three ways in, and they expose the same
operations under the same names, with the same parameters, results and error codes:

| Surface | Best for | Where |
|---|---|---|
| HTTP API | Any language, other hosts, phone apps | `http://127.0.0.1:8765/v1`, OpenAPI at `/v1/openapi.json` |
| Python `Client` | Python automations | `from roomba_kit import Client` |
| MCP | LLM agents | stdio: `roomba-kit mcp`; network: `http://<host>:8765/mcp` |

The operation list, with every parameter and result field, is in the
[operations reference](../reference/operations.md). Error codes are in the
[errors reference](../reference/errors.md).

## Tokens

Every endpoint except `GET /v1/health` needs `Authorization: Bearer <token>`. Mint one token
per consumer, so you can revoke one without touching the others:

```bash
roomba-kit token create scheduler     # prints the token once; store it in your secret manager
roomba-kit token list                 # names, created, last used; never the values
roomba-kit token revoke scheduler     # takes effect on the next request, no restart
```

Token admin is CLI-only and local to the service's machine, because a new token is a secret
shown once. Setup creates a `cli` token for this machine; the CLI and `roomba-kit mcp` read it
from `<config>/cli.token` (or `ROOMBA_KIT_API_TOKEN`). A missing, wrong or revoked token is
`401` with `{"error": {"code": "unauthorized", …}}`.

The service listens on `127.0.0.1` only. To reach it from another host, set `server.host` (see
[Deployment](../deployment.md)); tokens then travel over your LAN, so keep it on a network
you trust or put TLS in front of it.

## HTTP

Routes come from the registry (`contracts/surfaces.md`):

- System operations: `GET /v1/<operation>` for reads, `POST /v1/<operation>` for changes.
- Robot reads: `GET /v1/robots/{robot}/state`, `/settings`, `/position`, `/capabilities`, ….
- Commands: `POST /v1/robots/{robot}/commands/<command>` with a JSON body of params.
- Settings: `PUT /v1/robots/{robot}/settings/{setting}` with `{"value": …}`.

`{robot}` is a robot ID or an unambiguous name. GET parameters go in the query string, with lists
as repeated keys (`types=mission.started&types=mission.finished`); POST and PUT parameters go
in a JSON object body.

```bash
TOKEN=rk_…   # from token create
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/robots
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8765/v1/robots/Vacuum/commands/dock
```

`roomba-kit … --json` prints exactly the body the HTTP API returns for the same operation, so
you can prototype with the CLI and move to HTTP without reshaping anything.

### Events over SSE, with resume

`GET /v1/events` with `Accept: text/event-stream` streams events as they happen. Each frame has
`id: <seq>`, `event: <type>` and `data: <envelope JSON>`. Keep the last `seq` you handled, and
reconnect with `Last-Event-ID: <seq>` (or `?since=<seq>`): you get every event after it, then
live ones, with no gap and no repeat. Browsers' `EventSource` does this for you.

```bash
curl -N -H "Authorization: Bearer $TOKEN" -H "Accept: text/event-stream" \
  -H "Last-Event-ID: 1042" "http://127.0.0.1:8765/v1/events?types=mission.finished"
```

Without `since` or `Last-Event-ID` the stream is live only. The envelope, event types and the
page and long-poll alternatives are in [State and events](../state-and-events.md).

## Python `Client`

`roomba_kit.Client` is an async wrapper over the HTTP API with one method per operation, named
after it and returning its result model (`RobotState`, `CommandResult`, `Mission`, …).
Optional parameters default to `None`, meaning "the service's default".

```python
import asyncio

from roomba_kit import Client
from roomba_kit.core.events import EventType


async def main() -> None:
    # Defaults: http://127.0.0.1:8765 and this machine's `cli` token.
    async with Client("http://127.0.0.1:8765", token="rk_…") as kit:
        for robot in await kit.list_robots():
            state = await kit.get_state(robot.id)
            print(robot.name, state.activity, state.battery_pct)

        async for event in kit.events(types=[EventType.MISSION_FINISHED]):
            print(event.seq, event.data["mission"]["outcome"])


asyncio.run(main())
```

`events(since=None, *, robot=None, types=None, retry_s=1.0)` follows the SSE stream and
reconnects with `Last-Event-ID` after any drop, including a service restart, retrying every
`retry_s` while the service is unreachable. Pass `since` to start from a seq you saved; without
it the stream starts live. It ends only when you stop iterating, or with the `KitError` the
service returns (for example `unauthorized` or `robot_not_found`).

`kit.request("operation_name", {...})` runs any operation from its raw params and returns the
wire JSON, which is handy for generic tooling.

## MCP

The MCP server's tools are the operations, with descriptions that state what each does, its
parameters, which models support it and whether that's verified on hardware. Read-only tools
are marked `readOnlyHint`. Two kinds of operation are not offered over MCP, so secrets never
pass through an LLM: those that take a password (`cloud_login`, `set_robot_password`) and token
admin.

### Local agents: stdio

`roomba-kit mcp` serves the tools on stdin/stdout and proxies each call to the running service
with this machine's `cli` token. It never touches the robots itself. If the service isn't
running, every call fails with `service_unavailable: The Roomba Kit service isn't running…`.

A generic client config (Claude Desktop's `claude_desktop_config.json` and most other clients
use this shape):

```json
{
  "mcpServers": {
    "roomba-kit": {"command": "roomba-kit", "args": ["mcp"]}
  }
}
```

Claude Code: `claude mcp add roomba-kit -- roomba-kit mcp`. If `roomba-kit` isn't on the
client's `PATH`, use the absolute path, or `uv` with
`"command": "uv", "args": ["run", "--directory", "/path/to/roomba-kit", "roomba-kit", "mcp"]`.
Add `"--service-url", "http://…"` before `mcp` to proxy to a service on another port or host.

### Remote agents: HTTP at `/mcp`

The service mounts the same tools at `/mcp` (streamable HTTP), behind the same bearer auth.
Give each agent its own token:

```json
{
  "mcpServers": {
    "roomba-kit": {
      "type": "http",
      "url": "http://kit-host.lan:8765/mcp",
      "headers": {"Authorization": "Bearer rk_…"}
    }
  }
}
```

Claude Code: `claude mcp add --transport http roomba-kit http://kit-host.lan:8765/mcp
--header "Authorization: Bearer rk_…"`.

### Hermes Agent

[Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp) reads MCP
servers from `~/.hermes/config.yaml` under `mcp_servers`. On the service's machine, use stdio.
On another host, use the remote endpoint with a token of its own
(`roomba-kit token create hermes`), kept in `~/.hermes/.env` rather than the config file.
Hermes expands `${VAR}` placeholders from there:

```yaml
mcp_servers:
  roomba-kit:                       # on the service's machine
    command: roomba-kit
    args: ["mcp"]
  # or, from another host:
  # roomba-kit:
  #   url: "http://kit-host.lan:8765/mcp"
  #   headers:
  #     Authorization: "Bearer ${ROOMBA_KIT_TOKEN}"
```

Then ask it, for example, "clean the laundry room with the vacuum and tell me when it's done".
An agent finds the robot with `list_robots`, starts the clean, and calls `mission_wait` until
the mission ends.

## Errors, and `unconfirmed`

Every failure carries one code from the [catalogue](../reference/errors.md):

| Surface | Shape |
|---|---|
| HTTP | Status from the catalogue, body `{"error": {"code", "message", "robot_id"?, "details"?}}` |
| Python | `KitError` with `.code` (an `ErrorCode`), `.message`, `.robot_id`, `.details` |
| MCP | A tool error whose text starts with `<code>: <message>`, then the error JSON when there are details |
| CLI | The message on stderr and the catalogue's exit code; with `--json`, the error body on stdout |

Handle these deliberately:

- `not_supported`: the model lacks the capability (`details: {profile, capability}`). The robot
  was not contacted. Check `get_capabilities` first if you want to avoid it.
- `robot_offline` / `connection_held_elsewhere`: retry later. The second means another client
  (often the iRobot app) holds the robot's only local connection.
- `ambiguous_name` / `room_not_found`: `details.candidates` / `details.valid` list what would
  work, which is what an agent needs to recover on its own.
- `service_unavailable` (CLI, stdio MCP and `Client` only): the service isn't reachable.

**`unconfirmed` is an outcome, not a failure.** Commands wait for the robot's state to show the
command took effect. If it doesn't within the window, the command was still sent: HTTP answers
`202` with a normal `CommandResult` whose `outcome` is `"unconfirmed"`, `Client` and MCP return
that result without raising, and the CLI exits 3. Look at `result.state` or the next events
before deciding to retry: blindly resending `clean` to a robot that did start is a second
command.

Setting writes are confirmed by read-back. A write the robot accepted but didn't apply is the
error `not_applied` (`409`, `details.actual` holds the value the robot kept).

## Worked example: a scheduler

The robots' own schedules depend on the iRobot cloud. This script runs a whole-home clean on
weekday mornings from your own machine, waits for the mission, and reports the outcome. Run it
under cron, launchd or systemd.

```python
"""Weekday-morning clean. Run once a day at 09:00: `uv run python scheduler.py`."""

import asyncio
import datetime
import os
import sys

from roomba_kit import Client
from roomba_kit.core.errors import ErrorCode, KitError

ROBOT = "Vacuum"
RETRYABLE = {ErrorCode.ROBOT_OFFLINE, ErrorCode.CONNECTION_HELD_ELSEWHERE}


async def main() -> int:
    if datetime.date.today().weekday() >= 5:
        return 0  # weekend
    async with Client(token=os.environ["ROOMBA_KIT_API_TOKEN"]) as kit:
        state = await kit.get_state(ROBOT)
        if state.activity not in ("docked", "charging"):
            print(f"{ROBOT} is {state.activity}; skipping")
            return 0
        for attempt in range(3):
            try:
                started = await kit.clean(ROBOT)
                break
            except KitError as err:
                if err.code not in RETRYABLE or attempt == 2:
                    print(f"clean failed: {err.code}: {err.message}", file=sys.stderr)
                    return 1
                await asyncio.sleep(60)
        if started.outcome == "unconfirmed":
            print(f"clean sent but not confirmed yet (robot is {started.state.activity})")
        # Waits for the mission that's running, or the next one to start.
        result = await kit.mission_wait(ROBOT, timeout_s=3 * 3600)
        if getattr(result, "timeout", False):
            print("still running after 3 hours")
            return 1
        print(f"done: {result.outcome} in {result.duration_s} s")
        return 0


sys.exit(asyncio.run(main()))
```

The same flow over plain HTTP is `GET /v1/robots/Vacuum/state`, then
`POST /v1/robots/Vacuum/commands/clean`, then `GET /v1/robots/Vacuum/missions/wait?timeout_s=…`
(hold the request open; it returns when the mission ends). To clean specific rooms instead of
the whole home, use `clean_rooms` (see [Rooms](../rooms.md)).
