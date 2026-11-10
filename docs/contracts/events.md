# Contract: Events (schema `roomba-kit.event/v1`)

Envelope (every event):

```json
{"schema": "roomba-kit.event/v1", "seq": 1042, "type": "mission.finished",
 "robot_id": "<blid>", "at": "2026-11-08T20:44:12Z", "data": {}}
```

`seq` is global, strictly increasing and persisted. Consumers resume with `since=<last seq>`.
SSE frames carry `id: <seq>`, `event: <type>` and `data: <envelope>`, and honour `Last-Event-ID`.

| Type | When | `data` |
|---|---|---|
| `state.changed` | Any reported state field changes (coalesced to at most 1/s per robot) | `{changed: [field], state: RobotState}` |
| `activity.changed` | Normalized activity changes | `{from, to, mission_id?}` |
| `mission.started` | Cycle none→X | `{mission: Mission}` |
| `mission.finished` | Cycle X→none | `{mission: Mission}` (terminal outcome) |
| `robot.docked` | Activity becomes docked or charging after a mission | `{mission_id?}` |
| `robot.stuck` | Activity becomes stuck | `{error?: CodeText}` |
| `bin.full` | Bin full false→true | `{}` |
| `error.raised` / `error.cleared` | Error or not-ready code appears or clears | `{kind: error \| not_ready, code, text}` |
| `connection.changed` | ConnectionState changes | `{from, to, reason?}` |
| `setting.changed` | A setting value changes, from any source | `{setting, from, to}` |
| `cloud.status` | Cloud subsystem status changes | `{status, reason?}` |
| `cloud.timeline` | A cloud timeline report arrives | `{mission_id, kind: start \| travel \| room \| traversal \| evac \| fin \| other, room_id?, dest?, raw_type}` |
| `position` | Position fix (capable models, during missions) | `PositionFix` |
| `alert.dock_search_timeout` | Activity has been `returning` longer than `alerts.dock_search_minutes` (default 5), once per mission | `{mission_id, minutes}` |

Rules:
- Adding an event type or an optional field is non-breaking. Removing or renaming either one
  bumps the schema to `/v2`.
- `position` events are excluded from the persisted log by default (high volume). They are
  streamed live only, unless `events.persist_position=true`.
- Event payloads never contain secrets or raw transport payloads.
