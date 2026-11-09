# Mission history

The robots keep only lifetime counters, and the iRobot cloud keeps roughly the last 30
missions. The kit keeps its own record of every mission it sees, in `state.db` next to the
rest of its state, **indefinitely**. Nothing here needs the cloud.

History is built from the kit's [events](state-and-events.md): each `mission.started` and
`mission.finished` writes the mission's record, each `activity.changed` adds an activity
transition, and each `error.raised` during a mission is added to that mission's `errors`. A
mission appears as soon as it starts (with `outcome: "in_progress"`) and is completed when it
ends.

## What a mission looks like

```json
{
  "id": "01MISSIONA0000000000000000",
  "robot_id": "00000000000000000000000000000001",
  "started_at": "2026-11-08T20:13:20Z",
  "ended_at": "2026-11-08T20:44:12Z",
  "initiator": "kit",
  "rooms_requested": ["7"],
  "outcome": "completed",
  "duration_s": 1852,
  "errors": [],
  "cloud_mission_ref": "01MISSIONA0000000000000000"
}
```

| Field | Meaning |
|---|---|
| `id` | The robot's own mission ID. When the robot gives none, or reuses an earlier one, the kit assigns a UUID instead. |
| `initiator` | Who started it: `kit`, `schedule`, `button`, `app` (iRobot app or cloud), `team`, `other`. |
| `rooms_requested` | Room IDs from the clean request; empty means the whole home. Resolve names with [`list_rooms`](rooms.md). |
| `outcome` | `in_progress`, then one of `completed`, `cancelled`, `stuck`, `failed`, `manually_docked` or `unknown` (below). |
| `duration_s` | `ended_at − started_at`. `null` while in progress. |
| `errors` | Every robot error code (with readable text) raised during the mission, in order, each once. Not-ready codes aren't included. |
| `cloud_mission_ref` | The matching cloud mission, once the cloud's history shows it (below). Use it with `get_coverage`. |

### How the outcome is decided

The robot doesn't report an outcome locally, so the kit infers one when the mission ends,
taking the first rule that matches:

1. Ended on the dock straight from paused or stuck: `manually_docked` (someone carried it there).
2. The last command during the mission was `stop` or `dock`: `cancelled`.
3. The robot was stuck at the end: `stuck`.
4. An error code was showing at the end: `failed`.
5. Ended off the dock: `cancelled`; otherwise `completed`.

`unknown` means the mission ended while the kit wasn't watching: the service was stopped
mid-mission and, when it came back, the robot had already finished (or had moved on to another
mission). `ended_at` and `duration_s` are `null` because nobody saw the end.

## Restarts

Restarting the service mid-mission doesn't duplicate or lose the mission. When the service
comes back and the robot is still running under the same mission ID, the kit carries on with
the same record: same ID, same start time, and the errors raised before the restart are kept.
No second `mission.started` event is sent. Events published while history was catching up are
applied from the persisted event log, each exactly once.

A mission whose ID the kit had to make up (the robot gave none) can't be matched after a
restart, so it's closed as `unknown` and the rest of the run is recorded as a new mission.

## Cloud links

When the cloud is connected, the kit looks for recently finished missions in the cloud's
mission history every 15 minutes, for a day after each one ends, and records the cloud's ID in
`cloud_mission_ref`. A mission matches by ID, or (for kit-assigned IDs) by a cloud mission that
started within two minutes of it. This runs in the background: a slow or unavailable cloud never
delays history or anything local, and a mission the cloud never publishes simply keeps
`cloud_mission_ref: null`.

## Querying history

Two operations, on every surface. Both are read-only.

| Operation | Params | Result |
|---|---|---|
| `list_missions` | `robot?`, `since?`, `until?`, `outcome?`, `limit=50` (max 1000) | Missions, newest first |
| `get_mission` | `mission_id` | One mission |

- `robot` is a robot ID or unambiguous name. A robot you've since removed is still found by
  its ID.
- `since` and `until` filter on the start time: `since` is inclusive, `until` exclusive. Use
  ISO 8601 (`2026-11-01`, `2026-11-01T08:00:00+01:00`); a time with no offset is UTC.
- `outcome` is one of the outcomes above.
- An unknown `mission_id` fails with `invalid_request` (HTTP 400, CLI exit 2). The error
  catalogue has no "mission not found" code, and `mission_wait` treats an unknown ID the same way.

**CLI**

```bash
roomba-kit missions list
roomba-kit missions list --robot Vacuum --since 2026-11-01 --until 2026-12-01
roomba-kit missions list --outcome stuck --limit 10
roomba-kit missions show 01MISSIONA0000000000000000
roomba-kit --json missions list --robot Mop | jq '.[].outcome'
```

**HTTP**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/v1/missions?robot=Vacuum&since=2026-11-01&outcome=completed"
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8765/v1/missions/01MISSIONA0000000000000000
```

`GET /v1/missions` returns a JSON array of missions; `GET /v1/missions/{mission_id}` returns
one, or `400` with `{"error": {"code": "invalid_request", "details": {"mission_id": …}, …}}`.

**MCP**

Call `list_missions` with, for example, `{"robot": "Vacuum", "since": "2026-11-01",
"outcome": "stuck"}`, or `get_mission` with `{"mission_id": "01MISSIONA0000000000000000"}`.
Good questions for an agent: "how did each run end this week", "which missions got stuck, and
on which errors".

## Activity transitions

Every activity change (for example `docked` → `cleaning` → `returning` → `docked`) is stored
with its robot, time and mission ID. V1 has no operation that lists them; the same changes are
available as `activity.changed` events through `events_list` for as long as events are kept
(`history.event_retention_days`).

## Waiting for a mission

To block until the current mission ends, use `mission_wait` (`roomba-kit missions wait ROBOT`);
see [state and events](state-and-events.md). It returns the same `Mission` shape.
