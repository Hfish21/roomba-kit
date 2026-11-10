# Cloud extras

Everything in the kit works without an iRobot account. If you add one, the kit uses the
iRobot cloud for a few extras the robots don't offer locally. When the cloud is down, slow or
rejects the login, those extras say so (`cloud_unavailable`), and nothing else changes: robot
commands, state and events never wait on the cloud.

## What an account adds

| Extra | Operation / event | Capability | Notes |
|---|---|---|---|
| Floor plan: rooms with names, types and outlines | `get_floor_plan` | `cloud.rooms` | Cached; served from cache while the cloud is down |
| The cloud's recent mission history (about 30 missions) | `cloud_history` | `cloud.history` | Always live; `cloud_unavailable` while the cloud is down |
| Where a finished mission cleaned, with dirt and clutter | `get_coverage` | `cloud.coverage` | Published minutes after the robot docks |
| Live room-level mission progress | `cloud.timeline` events | `cloud.timeline` | Heading to a room, in a room, heading home, bin emptied, finished |
| Robot password retrieval without the button dance | `cloud_retrieve_password` | — | See [setup](setup.md) |

Both supported models declare all four cloud capabilities. The m6's `cloud.timeline` is still
**unverified**: the kit watches it, but no m6 timeline message has been seen on hardware yet.
See the [support matrix](reference/support-matrix.md).

## The unofficial-API caveat

iRobot has no public cloud API. The kit talks to the same endpoints the iRobot app uses, through
a reverse-engineered client library, and iRobot (now owned by Picea) can change or close them
at any time. That's why the cloud is optional by design: treat these extras as
nice to have, and never build an automation that needs one to control a robot. If the cloud
API changes, the extras stop working and report `cloud_unavailable`; local control doesn't
notice.

## Adding and removing the account

```bash
roomba-kit cloud login            # prompts for email and password; saved only if login works
roomba-kit cloud status           # connected | unavailable (reason) | not_configured
roomba-kit cloud logout           # forgets the stored login and disconnects
```

Over HTTP, `POST /v1/cloud_login` with `{"username", "password", "country"}`. `cloud_login`
isn't an MCP tool, so an agent never handles the account password; `cloud_status` and
`cloud_logout` are.

The login can also come from `ROOMBA_KIT_CLOUD__USERNAME` / `ROOMBA_KIT_CLOUD__PASSWORD`.
`cloud logout` can't remove those: unset them and restart the service. To switch the cloud off
completely, even with credentials present, set `[cloud] enabled = false` in `config.toml`.

After you remove the account, floor plans, history and coverage already fetched stay in the
kit's state database and are still served (with their `retrieved_at`). Nothing new is fetched,
and the timeline stops.

## Floor plan

```bash
roomba-kit floor-plan Vacuum
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/robots/Vacuum/floor-plan
```

MCP: the `get_floor_plan` tool with `{"robot": "Vacuum"}`.

```json
{
  "map": {"map_id": "<map id>", "version": "<map version>"},
  "rooms": [
    {"id": "7", "name": "Laundry Room", "type": "laundry_room",
     "polygon": [[[-3.67, 5.98], [-3.67, 4.51], [-2.20, 4.51], [-2.20, 5.98]]]}
  ],
  "retrieved_at": "2026-11-08T20:41:07.123456Z"
}
```

- The map is the robot's active stored map.
- `polygon` is a list of rings of `[x, y]` metres in the robot's map frame (the dock is near
  the origin). The first ring is the outline; any more are holes.
- `name` is the name you gave the room in the kit, else the cloud's name, else `Room <id>`.

## Cloud history

```bash
roomba-kit cloud history Vacuum --limit 5
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/v1/robots/Vacuum/cloud-history?limit=5"
```

MCP: `cloud_history` with `{"robot": "Vacuum", "limit": 5}`. You get kit-shaped `CloudMission`
records, newest first: `mission_ref`, `number`, `started_at`, `ended_at`, `duration_s`,
`outcome` (`completed`, `cancelled`, `stuck`, `manually_docked`, `unknown`), `initiator`,
`area_m2`, `rooms_requested`, `evacs`, `map` and `retrieved_at`. The cloud keeps about the 30
most recent missions. For a complete record of everything the kit has seen, use the kit's own
mission history (`missions list`), which doesn't need the cloud.

## Coverage

```bash
roomba-kit coverage Vacuum <mission id>
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8765/v1/robots/Vacuum/missions/<mission id>/coverage
```

MCP: `get_coverage` with `{"robot": "Vacuum", "mission_id": "<mission id>"}`. The mission ID
is the kit's mission ID (which is the robot's own mission ID) or the cloud's `mission_ref`;
they're the same value for missions the robot numbered.

| Field | Meaning |
|---|---|
| `points` | Centres of the covered grid cells, `[x, y]` metres |
| `point_area_m2` | Area of one cell |
| `dirt` | Cells where the robot detected dirt |
| `clutter` | Clutter areas, flattened to points: the vertices of each area's outline, one area after another. Area boundaries aren't kept. |
| `map_id`, `map_version` | The map the coordinates belong to |

**Coverage lags.** A mission isn't in the cloud while it runs, and in testing it was still
missing two minutes after the robot docked. Until it appears, `get_coverage` returns
`cloud_unavailable` with reason `not_yet_published`; retry in a few minutes. Missions older than
the cloud's ~30 most recent also report `not_yet_published` unless the kit fetched them earlier.
Once fetched, a mission's coverage is kept and served from the kit without asking the cloud
again.

## Live timeline

While the cloud is connected, the kit follows each robot's mission timeline and publishes every
new entry once as a `cloud.timeline` event:

```bash
roomba-kit events watch --type cloud.timeline
```

```json
{"type": "cloud.timeline", "robot_id": "00000000000000000000000000000001",
 "data": {"mission_id": "<mission id>", "kind": "travel", "room_id": "7", "dest": "room", "raw_type": "travel"}}
```

| `kind` | Means | Extra fields |
|---|---|---|
| `start` | Mission started | |
| `travel` | Heading somewhere | `dest` (`room` or `dock`), `room_id` when heading to a room |
| `room` | Cleaning a room | `room_id` |
| `traversal` | Passing through a room | `room_id` |
| `evac` | Clean Base emptying the bin | |
| `fin` | Mission finished | |
| `other` | Anything else the cloud reports (pause, bin full, …) | `raw_type` names it |

Entries arrive within seconds of the robot reporting them. If the kit loses the cloud mid-mission,
it catches up on entries it missed when it reconnects, without repeating any. It doesn't replay
what happened before it started watching. All robots share one cloud connection (two
connections under one account evict each other).

## When the cloud is unavailable

| Situation | `cloud_status` | Cloud operations |
|---|---|---|
| No account configured | `not_configured` | `cloud_unavailable`, reason `not_configured` |
| Cloud unreachable | `unavailable`, reason e.g. `connection_failed`, `timeout` | Cached copy if there is one, else `cloud_unavailable` with that reason |
| Login rejected | `unavailable`, reason `auth_failed` | Same; the kit stops retrying until the credentials change |
| Mission not published yet | `connected` | `get_coverage`: `cloud_unavailable`, reason `not_yet_published` |

`cloud_unavailable` is HTTP 503 and CLI exit 9; the reason is in `error.details.reason`. The
kit retries the login in the background, backing off to at most 15 minutes, and publishes each
change as a `cloud.status` event. Each cloud request has its own time limit, so a hung cloud
fails that one request without delaying anything else.
