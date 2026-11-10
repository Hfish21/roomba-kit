# Data Model: Roomba Kit V1

Kit-owned types only. No transport library types appear here (Constitution V). All public
models are versioned with the contract (`v1`).

## Robot

| Field | Type | Rules |
|---|---|---|
| `id` | str | The robot's BLID. Permanent; the only identifier (FR-020) |
| `name` | str | Display name from the robot; may be duplicated across robots |
| `profile_id` | str \| null | e.g. `roomba_i5`, `braava_m6`; null when unsupported |
| `supported` | bool | false when no profile matches the SKU (FR-024) |
| `sku` | str | e.g. `i5G5020` |
| `firmware` | str | e.g. `daredevil+2.6.0+…` |
| `address` | str | Last known IP; updated by rediscovery (FR-018) |
| `connection` | ConnectionState | See below |
| `capabilities` | list[CapabilityStatus] | From the profile |
| `guide` | str \| null | `docs/guides/adding-a-model.md` when `supported` is false, else null (FR-024). Also on `DiscoveredRobot` |

**ConnectionState**: `connected`, `connecting`, `offline`, `held_elsewhere`, `auth_failed`,
`not_configured`.

Transitions:
- `not_configured` → `connecting` when credentials are saved.
- `connecting` → `connected`, or to `held_elsewhere`, `auth_failed` or `offline` on the
  corresponding error.
- `connected` → `offline` on drop; → `held_elsewhere` when another client takes the robot's
  connection; → `auth_failed` when the robot rejects the password; → `connecting` when the
  robot's credentials change (reconnect with the new ones).
- `offline` / `held_elsewhere` → `connecting` on the backoff tick.
- `auth_failed` is terminal until credentials change.

## ModelProfile (code, not stored)

| Field | Type | Rules |
|---|---|---|
| `id` | str | Unique, snake_case |
| `display_name` | str | e.g. "Roomba i5/i5+" |
| `sku_patterns` | list[regex] | Matched against discovery `sku` |
| `capabilities` | dict[Capability, Verification] | `verified` or `unverified` (FR-021) |
| `settings` | dict[SettingName, SettingMapping] | Protocol keys, encode/decode, write group |
| `command_overrides` | dict[Command, CommandSpec] | Confirmation predicate or window overrides |
| `error_texts` | dict[int, str] | Readable error and not-ready text (FR-026) |

**Capability** (enum, V1):

| Group | Capabilities |
|---|---|
| Commands | `clean`, `clean_rooms`, `pause`, `resume`, `stop`, `dock`, `find`, `evac`, `reboot`, `train` |
| Settings | `setting.passes`, `setting.edge_clean`, `setting.always_finish`, `setting.child_lock`, `setting.eco_charge`, `setting.schedule_hold`, `setting.pad_wetness`, `setting.spray_overlap`, `setting.carpet_boost` |
| Telemetry | `state.bin`, `state.clean_base`, `state.pad`, `state.tank`, `state.lid` |
| Live data | `live_position` |
| Cloud | `cloud.rooms`, `cloud.history`, `cloud.coverage`, `cloud.timeline` |

**Verification**: `verified` (with a cited run) or `unverified`.

V1 profiles. ✅ verified, ⚪ unverified, — not supported:

| Capability | roomba_i5 | braava_m6 |
|---|---|---|
| clean, pause, resume, dock, find | ✅ | ✅ |
| stop, reboot, train | ⚪ | ⚪ |
| clean_rooms | ✅ | ⚪ |
| evac, state.bin, state.clean_base | ✅ | — |
| state.pad, state.tank, state.lid | — | ✅ |
| setting.passes, edge_clean, child_lock, eco_charge, schedule_hold | ⚪ | ⚪ |
| setting.always_finish | ⚪ | — |
| setting.pad_wetness, spray_overlap | — | ⚪ |
| setting.carpet_boost | — (firmware ignores writes) | — |
| live_position | — (verified absent) | ✅ |
| cloud.rooms, cloud.history, cloud.coverage | ✅ | ✅ |
| cloud.timeline | ✅ | ⚪ |

## RobotState (snapshot)

| Field | Type | Notes |
|---|---|---|
| `robot_id` | str | |
| `at` | datetime (UTC) | Time of the last update |
| `battery_pct` | int 0–100 | |
| `activity` | Activity | Normalized (research R8) |
| `mission` | MissionRef \| null | `id`, `started_at`, `initiator` |
| `last_command` | CommandEcho \| null | `command`, `at`, `initiator` |
| `error` | CodeText \| null | `code`, `text` |
| `not_ready` | CodeText \| null | |
| `signal` | {rssi, snr} | |
| `map` | MapRef \| null | `map_id`, `version` |
| `schedules` | list[RobotSchedule] | Read-only (FR-026) |
| `counters` | {missions_ok, missions_failed, missions_cancelled, runtime_hours, stuck_count} | |
| `bin` | {present, full} \| null | Profile-gated |
| `clean_base` | {present, state} \| null | Profile-gated |
| `pad` | {detected} \| null | Profile-gated |
| `tank` | {present, level_pct} \| null | Profile-gated |
| `lid_open` | bool \| null | Profile-gated |
| `settings` | dict[SettingName, value] | Current values for supported settings |

**Activity**: `docked`, `charging`, `cleaning`, `mapping`, `paused`, `returning`,
`emptying_bin`, `stuck`, `unknown`.

**Initiator**: `kit` (sent by the kit; raw `localApp` from this client), `schedule`,
`button`, `app` (iRobot app or cloud), `team` (vendor routine), `other`.

## Map, Room, LearnedRoom

- **Map**: `robot_id`, `map_id`, `version`, `source` (`robot` | `cloud`), `retrieved_at`.
- **Room**: `robot_id`, `map_id`, `map_version`, `room_id` (str), `cloud_name` (nullable),
  `local_name` (nullable), `type` (nullable). The effective name is `local_name`, then
  `cloud_name`, then `Room <id>`.
- **LearnedRoom**: a room ID observed in a robot's `lastCommand` without the cloud (FR-032),
  with `first_seen` and `last_seen`.
- **Validation**:
  - Names are unique per robot and map version, case-insensitively.
  - A clean request must reference the robot's current `map.version`, otherwise it fails with
    `map_changed` (FR-031).
  - An empty room list is rejected.

## Command & CommandResult

- **Command request**: `robot` (id or unambiguous name), `command`, `params` (typed per
  command; `clean_rooms` takes `rooms: list[RoomRef]`, `ordered: bool = true`, and
  `room_options` where supported).
- **CommandResult**:

  | Field | Type |
  |---|---|
  | `robot_id` | str |
  | `command` | str |
  | `outcome` | `confirmed` \| `unconfirmed` |
  | `state` | RobotState summary (activity, mission) |
  | `waited_ms` | int |

  Errors are returned as Error instead of a CommandResult.

## SettingChange

Fields: `robot_id`, `setting`, `requested`, `outcome` (`applied` | `not_applied`), `actual`.

## Mission (local history)

| Field | Type | Notes |
|---|---|---|
| `id` | str | Robot mission ID when available, else a kit UUID |
| `robot_id` | str | |
| `started_at` / `ended_at` | datetime | |
| `initiator` | Initiator | |
| `rooms_requested` | list[room_id] | Empty means whole home |
| `outcome` | `in_progress`, `completed`, `cancelled`, `stuck`, `failed`, `manually_docked`, `unknown` | |
| `duration_s` | int | |
| `errors` | list[CodeText] | |
| `cloud_mission_ref` | str \| null | Links to cloud coverage when published |

**Lifecycle**: `in_progress` → a terminal outcome at the end of the cycle. A service restart
mid-mission resumes the same record by mission ID (FR-044, US8 scenario 3).

## ActivityTransition

Fields: `robot_id`, `at`, `from`, `to`, `mission_id`. Recorded from every `activity.changed`
event and readable through `get_mission` (FR-045).

## MissionDetail (`get_mission` result)

A `Mission` plus `transitions: list[ActivityTransition]`: the mission's activity changes, ordered
by time (additive; `list_missions` and mission events still carry plain `Mission`).

## Event

| Field | Type |
|---|---|
| `schema` | `"roomba-kit.event/v1"` |
| `seq` | int, monotonic, global |
| `type` | EventType |
| `robot_id` | str \| null |
| `at` | datetime UTC |
| `data` | type-specific object |

**EventType**: `state.changed`, `activity.changed`, `mission.started`, `mission.finished`,
`robot.docked`, `robot.stuck`, `bin.full`, `error.raised`, `error.cleared`,
`connection.changed`, `setting.changed`, `cloud.timeline`, `cloud.status`, `position`,
`alert.dock_search_timeout`. Full payloads are in `contracts/events.md`.

## PositionFix

Fields: `robot_id`, `at`, `x_m`, `y_m`, `heading_rad`, `map_id`, `map_version`. A separate
`no_fix` result is not an error.

## CoverageRecord (cloud)

Fields: `robot_id`, `mission_ref`, `map_id`, `map_version`, `points` (list[[x, y]] metres),
`point_area_m2`, `dirt` (list[[x, y]]), `clutter` (list[[x, y]]), `retrieved_at`.

## Credentials (never serialized to any surface)

| Kind | Stored as |
|---|---|
| RobotCredential | `robot_id`, `password` |
| CloudCredential | `username`, `password`, `country` |
| ApiToken | `name`, `sha256`, `created_at`, `last_used_at` (only `name`, `created_at` and `last_used_at` are ever displayed) |

## Error

`{code: ErrorCode, message: str, robot_id?: str, details?: object}`. Codes are in
`contracts/errors.md`.

## VerificationRun (`verify` result)

| Field | Type | Notes |
|---|---|---|
| `id` | str | Run ID |
| `robot_id` | str | |
| `profile_id` | str | |
| `anchor` | str | `run-YYYYMMDD-HHMMSS`; profiles cite `docs/verification/<profile>.md#<anchor>` |
| `started_at` / `ended_at` | datetime | |
| `allow_motion` | bool | |
| `results` | list[{capability, result: pass \| fail \| skipped, evidence}] | |
| `report` | str | Markdown appended by `verify --write-report` (no IDs, names or addresses) |
