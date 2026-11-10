# State and events

Every robot's live picture is available on demand (`get_state`), and every meaningful change is
pushed as a typed event you can stream, page through or wait on. Same names on every surface:

| Need | HTTP | CLI | MCP tool |
|---|---|---|---|
| Current state | `GET /v1/robots/{robot}/state` | `roomba-kit state ROBOT` | `get_state` |
| Page of past events | `GET /v1/events?since=N` | `roomba-kit events list --since N` | `events_list` |
| Live stream | `GET /v1/events` + `Accept: text/event-stream` | `roomba-kit events watch` | — (use `events_wait`) |
| Wait for the next events | `GET /v1/events/wait?since=N` | `roomba-kit events wait --since N` | `events_wait` |
| Wait for a mission to end | `GET /v1/robots/{robot}/missions/wait` | `roomba-kit missions wait ROBOT` | `mission_wait` |

`robot` is a robot ID or an unambiguous name, everywhere.

## State

`get_state` returns a `RobotState` (field reference: `docs/reference/operations.md`):

| Field | Meaning |
|---|---|
| `at` | When the robot last reported. While it's offline you get the last state it reported, so check `at` (or `get_robot().connection`) before trusting it. |
| `battery_pct`, `activity` | See [activity](#activity) below. |
| `mission` | The robot's current mission: `id`, `started_at`, `initiator` (`kit`, `schedule`, `button`, `app`, `team`, `other`). `null` when idle. |
| `last_command` | The last command the robot accepted from *any* client, with its rooms and map for a room clean. |
| `error`, `not_ready` | `{code, text}`. The text comes from the robot's profile, then a shared table, else `Error <code>`. Not-ready 23 and 39 are transient lifecycle states that clear on their own; 68 means "Not ready yet (transient around docking and bin emptying)". |
| `signal`, `map`, `schedules`, `counters` | Wi-Fi RSSI/SNR, the stored map's ID and version, robot-stored schedules (read-only), lifetime counters. |
| `bin`, `clean_base`, `pad`, `tank`, `lid_open` | Only for models that have them (`null` otherwise). |
| `settings` | Current value of each setting the model supports. |

An unsupported model answers `unsupported_model`; a robot that has never reported answers why
(`robot_offline`, `connection_held_elsewhere`, `auth_failed`).

## Activity

| `activity` | The robot is… |
|---|---|
| `docked` / `charging` | On its dock, full / below 100 %. |
| `cleaning` / `mapping` | Running a mission / a mapping run. |
| `paused` | Mid-mission, stopped where it is. |
| `returning` | Heading to the dock (end of mission, low battery, or told to). |
| `emptying_bin` | Emptying into the Clean Base. |
| `stuck` | Needs help. |
| `unknown` | A state the kit can't classify, typically the second or two between commands. |

Robots flicker: on dock contact an i5 or m6 can report `cleaning` for a moment between
`returning` and `charging`. The kit reports what the robot says.

## Events

Every event has the same envelope (schema `roomba-kit.event/v1`):

```json
{"schema": "roomba-kit.event/v1", "seq": 1042, "type": "mission.finished",
 "robot_id": "00000000000000000000000000000001", "at": "2026-11-08T20:44:12Z",
 "data": {"mission": {"id": "01M6Y7RSW0T3M943E9K42Q03P1", "outcome": "completed", "…": "…"}}}
```

`seq` is global and strictly increasing, across restarts. Filter with `robot` and `types`
(repeat the parameter: `types=mission.started&types=mission.finished`).

| Type | When | `data` |
|---|---|---|
| `state.changed` | Any state field changed (at most once a second per robot) | `{changed: [field], state}` |
| `activity.changed` | Activity changed between two known values (`unknown` blips are skipped) | `{from, to, mission_id}` |
| `mission.started` | A mission is seen running | `{mission}` |
| `mission.finished` | It ended | `{mission}` with its outcome |
| `robot.docked` | Arrived on the dock | `{mission_id}` |
| `robot.stuck` | Became stuck | `{error}` |
| `bin.full` | The bin became full | `{}` |
| `error.raised` / `error.cleared` | An error or not-ready code appeared / went away | `{kind: error \| not_ready, code, text}` |
| `connection.changed` | The kit's link to the robot changed | `{from, to, reason}` |
| `setting.changed` | A setting changed, from any client (first reads after connecting aren't changes) | `{setting, from, to}` |
| `cloud.status`, `cloud.timeline`, `position` | See [Cloud extras](cloud.md) and [Live position](position.md) | |
| `alert.dock_search_timeout` | See [alerts](#alerts) | `{mission_id, minutes}` |

A room clean that docks on its own produces (abridged):

```
mission.started    {mission: {id: M1, initiator: kit, rooms_requested: ["7"], outcome: in_progress}}
activity.changed   {from: docked, to: cleaning, mission_id: M1}
activity.changed   {from: cleaning, to: returning, mission_id: M1}
activity.changed   {from: returning, to: docked, mission_id: M1}
robot.docked       {mission_id: M1}
mission.finished   {mission: {id: M1, outcome: completed, duration_s: 1032, …}}
```

### Missions

- A mission starts when the robot is **seen running** (cleaning, mapping, paused, returning or
  stuck) after its cycle begins. A cycle that never runs, such as a dock-side bin evacuation or
  a start that fails, isn't a mission and produces no mission events.
- `id` is the robot's own mission ID, or a kit UUID if the robot gave none or reused one.
- `rooms_requested` is the room list of the room clean that started it; empty means whole home.
- `outcome`, first rule that matches:

  | Outcome | When |
  |---|---|
  | `manually_docked` | It ended on the dock straight from paused or stuck: someone carried it there. |
  | `cancelled` | The last command during the mission was `stop` or `dock`, or it ended off the dock. |
  | `stuck` | It was stuck when it ended. |
  | `failed` | It ended with an error code. |
  | `completed` | Otherwise: it made its own way home. |

  These come from local state only. The iRobot app's own verdict (in cloud history) can differ.
- Each `mission.started` / `mission.finished` carries the whole mission, so a consumer can keep
  its own history from events alone.

## Resuming

Consumers keep the last `seq` they handled and resume from it. Nothing is lost while they're
away, as long as it's within the retention window (`history.event_retention_days`, default 30).

- **Page:** `GET /v1/events?since=N&limit=100` → `{events, next}`; call again with `since=next`.
- **Long poll:** `GET /v1/events/wait?since=N&timeout_s=30` returns as soon as anything after
  `N` exists, or `{events: [], next: N}` at the timeout (max 300 s). This is the MCP way to
  follow events.
- **Stream:** `GET /v1/events` with `Accept: text/event-stream`. Frames look like

  ```
  id: 1042
  event: mission.finished
  data: {"schema":"roomba-kit.event/v1","seq":1042,…}
  ```

  Reconnect with `Last-Event-ID: <last id>` (what browsers' `EventSource` does on its own) or
  `?since=N`; you get everything after it, then live events, with no gap or repeat. With
  neither, the stream is live only. The server pings every 15 s; a client that falls 10,000
  events behind is disconnected and should resume the same way.

  ```bash
  curl -N -H "Authorization: Bearer $TOKEN" -H "Accept: text/event-stream" \
    "http://127.0.0.1:8765/v1/events?types=mission.finished"
  ```

- **CLI:** `roomba-kit events watch [--robot R] [--type T …] [--since N]` prints one line per
  event (`--json`: one envelope per line) and reconnects with the last seq on its own.

Events reach subscribers within 2 seconds of the robot reporting the change (SC-005; the kit
publishes them on the same path that processes the robot's message).

### Waiting for a mission

`mission_wait(robot, mission_id?, timeout_s=1800)` returns the finished `Mission`. Without
`mission_id` it waits for the current mission, or the next one if none is running yet (so it's
safe to call right after `clean`). At the timeout it returns `{timeout: true, mission}` with the
mission still in progress. A `mission_id` that isn't the current or a recent mission is
`invalid_request`. Recent finished missions are remembered in memory only; for older ones use
[mission history](history.md).

## Alerts

`alert.dock_search_timeout` fires when a robot has been `returning` for longer than
`alerts.dock_search_minutes` (default 5), at most once per mission. It exists because the i5 can
hunt for its dock for an hour. Brief flickers out of `returning` (under 10 s) don't restart the
clock. Set the threshold in `config.toml`:

```toml
[alerts]
dock_search_minutes = 5
```
