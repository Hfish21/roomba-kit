# Contract: Operation Registry (v1)

This is the single source of truth. The HTTP routes, MCP tools and CLI commands are all
generated from it (research R1). An operation's name is identical on every surface. Robot
parameters accept a robot ID or an unambiguous display name (FR-020).

Legend: **Cap** = the required profile capability (`—` means none). **Surfaces**: H = HTTP,
M = MCP, C = CLI. Every operation is HCM unless marked otherwise.

## System & setup

| Operation | Params | Result | Cap | Surfaces |
|---|---|---|---|---|
| `health` | — | `{status, version}` | — | HCM (HTTP route needs no auth) |
| `service_status` | — | `{version, uptime_s, robots:[{id, connection}], cloud:{status, reason?}}` | — | HCM |
| `discover_robots` | `broadcast?: str`, `timeout_s=8` | `[DiscoveredRobot{id, name, sku, firmware, address, supported, profile_id?, configured, guide?}]` | — | HCM |
| `add_robot` | `address: str` | `DiscoveredRobot` | — | HCM |
| `retrieve_password` | `robot`, `timeout_s=60` | `{outcome: stored \| timeout \| cloud_only, steps:[str]}` (password never returned) | — | HCM |
| `cloud_retrieve_password` | `robot` | `{outcome: stored \| unavailable, reason?}` | — | HCM |
| `set_robot_password` | `robot`, `password` | `{outcome: stored \| auth_failed}` | — | HC (not M; see plan Complexity Tracking) |
| `remove_robot` | `robot` | `{removed: bool}` | — | HCM |
| `cloud_login` | `username`, `password`, `country="US"` | `{status: connected \| failed, reason?}` | — | HC (not M) |
| `cloud_logout` | — | `{status}` | — | HCM |
| `cloud_status` | — | `{status: connected \| unavailable \| not_configured, reason?, last_ok?}` | — | HCM |
| `config_show` | — | Config with every secret replaced by `"***"` | — | HCM |
| `token_create` / `token_list` / `token_revoke` | `name` | Created token shown once; list shows name, created, last used | — | **C only** (local file op) |

## Inventory & state

| Operation | Params | Result | Cap |
|---|---|---|---|
| `list_robots` | — | `[Robot]` | — |
| `get_robot` | `robot` | `Robot` | — |
| `get_state` | `robot` | `RobotState` | — |
| `get_capabilities` | `robot` | `[CapabilityStatus{capability, verification}]` | — |

## Commands (each returns `CommandResult`, research R9)

| Operation | Params | Cap |
|---|---|---|
| `clean` | `robot` | `clean` |
| `clean_rooms` | `robot`, `rooms: [str]` (IDs or names), `ordered=true`, `room_options?` | `clean_rooms` |
| `pause` / `resume` / `stop` | `robot` | same name |
| `dock` | `robot` | `dock` |
| `find` | `robot` | `find` |
| `evac` | `robot` | `evac` |
| `reboot` | `robot` | `reboot` |
| `train` | `robot` | `train` |

## Settings

| Operation | Params | Result | Cap |
|---|---|---|---|
| `get_settings` | `robot` | `{setting: value}` for supported settings | — |
| `set_setting` | `robot`, `setting`, `value` | `SettingChange` | `setting.<name>` |

Setting values:
- `passes` ∈ {`auto`, `one`, `two`}
- `edge_clean`, `always_finish`, `child_lock`, `eco_charge`, `schedule_hold` are booleans
- `pad_wetness` ∈ {1, 2, 3}
- `spray_overlap` ∈ {`standard`, `deep`, `extended`}

## Rooms & maps

| Operation | Params | Result | Cap |
|---|---|---|---|
| `list_rooms` | `robot` | `{map: MapRef, rooms: [Room], stale: bool}` | — |
| `import_rooms` | `robot` | `{imported: int, map: MapRef}` | `cloud.rooms` |
| `name_room` | `robot`, `room_id`, `name` | `Room` | — |
| `get_floor_plan` | `robot` | `{map, rooms:[{id, name, polygon}], retrieved_at}` | `cloud.rooms` |

## Position

| Operation | Params | Result | Cap |
|---|---|---|---|
| `get_position` | `robot` | `PositionFix` or `{no_fix: true}` | `live_position` |

The position stream is delivered as `position` events (see `events.md`).

## Events

| Operation | Params | Result |
|---|---|---|
| `events_list` | `since?: int`, `robot?`, `types?: [str]`, `limit=100` | `{events: [Event], next: int}` |
| `events_wait` | `since: int`, `robot?`, `types?`, `timeout_s=30` (max 300) | `{events: [Event], next: int}` (may be empty) |
| `mission_wait` | `robot`, `mission_id?`, `timeout_s=1800` | `Mission` (terminal) or `{timeout: true, mission}` |

HTTP also exposes `events_list` as an SSE stream at the same route with
`Accept: text/event-stream`. This is presentation only, not a separate operation.

## History & cloud records

| Operation | Params | Result | Cap |
|---|---|---|---|
| `list_missions` | `robot?`, `since?`, `until?`, `outcome?`, `limit=50` | `[Mission]` | — |
| `get_mission` | `mission_id` | `Mission` | — |
| `cloud_history` | `robot`, `limit=30` | `[CloudMission]` | `cloud.history` |
| `get_coverage` | `robot`, `mission_id` | `CoverageRecord` or `cloud_unavailable(reason=not_yet_published)` | `cloud.coverage` |

## Verification

| Operation | Params | Result |
|---|---|---|
| `verify` | `robot`, `capabilities?: [str]` (omit for every declared capability; `[]` is `invalid_request`), `allow_motion=false` | `VerificationRun{id, robot_id, profile_id, anchor, started_at, ended_at, allow_motion, results: [{capability, result: pass \| fail \| skipped, evidence}], report}` |

Motion checks are skipped unless `allow_motion=true`. The CLI prompts before moving the robot.
Naming a capability the model doesn't declare is `not_supported`, before any robot contact.
`report` is markdown for `docs/verification/<profile_id>.md`; `anchor` (`run-YYYYMMDD-HHMMSS`,
UTC) is what a profile's `Verification.verified(run=...)` cites.

## CLI composites (presentation only)

`roomba-kit setup` is an interactive wizard. It calls only the operations above
(`discover_robots`, `add_robot`, `retrieve_password`, `cloud_login`,
`cloud_retrieve_password`, `import_rooms`) and adds prompts and explanations, with no
behaviour of its own (Constitution III).
