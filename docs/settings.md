# Settings

Roomba Kit reads and changes the settings each robot stores, and only reports a change as
applied once the robot reports the new value back.

!!! warning "Unverified"
    Every setting write is `unverified` on both models until a `roomba-kit verify` run passes on
    real hardware (see [How `verify` works](#how-verify-works)). Reads are taken from the state
    the robots publish, which was captured on hardware.

## Settings per model

| Setting | Values | Roomba i5/i5+ | Braava jet m6 |
|---|---|---|---|
| `passes` | `auto`, `one`, `two` | yes | yes |
| `edge_clean` | `true`, `false` | yes | yes |
| `always_finish` | `true`, `false` (keep cleaning when the bin is full) | yes | — |
| `child_lock` | `true`, `false` | yes | yes |
| `eco_charge` | `true`, `false` | yes | yes |
| `schedule_hold` | `true`, `false` (pause the robot's own schedules) | yes | yes |
| `pad_wetness` | `1`, `2`, `3` | — | yes |
| `spray_overlap` | `standard`, `deep`, `extended` | — | yes |

A setting marked — is `not_supported` on that model, and the kit never contacts the robot for
it. `carpet_boost` is not offered on the i5: it accepts the write and drops it.

Notes:

- `passes` is one logical setting the robot stores as two values. The kit always writes both
  together, so you never set half of it.
- `pad_wetness` is stored per pad type (disposable and reusable). The kit sets both to the same
  value. If the iRobot app has set them differently, the kit reads `null` until you set it.
- A setting reads as `null` when the robot hasn't reported it.

## Reading settings

```bash
roomba-kit settings show Vacuum
```

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/robots/Vacuum/settings
```

```json
{"passes":"one","edge_clean":true,"always_finish":false,"child_lock":false,"eco_charge":false,"schedule_hold":false}
```

MCP: call `get_settings` with `{"robot": "Vacuum"}`.

## Changing a setting

```bash
roomba-kit settings set Vacuum passes two
roomba-kit settings set Mop pad_wetness 2
roomba-kit settings set Mop schedule_hold true      # true/false, on/off, yes/no, 1/0
```

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"value": "two"}' http://127.0.0.1:8765/v1/robots/Vacuum/settings/passes
```

```json
{"robot_id":"…","setting":"passes","requested":"two","outcome":"applied","actual":"two"}
```

MCP: call `set_setting` with `{"robot": "Vacuum", "setting": "passes", "value": "two"}`.

Writes to one robot run in its command queue, so a setting sent after a command is applied
after that command, in order. The kit waits up to 10 seconds for the robot to report the new
value.

## When a write isn't applied

Some firmware accepts a setting and silently ignores it. The kit never trusts the write: if
the robot doesn't report the requested value within 10 seconds, the result is the
`not_applied` error, carrying what the robot actually holds:

| Surface | Result |
|---|---|
| HTTP | `409`, `{"error": {"code": "not_applied", "details": {…SettingChange…, "actual": "one"}}}` |
| CLI | the message and details on stderr, exit code `3` |
| MCP | a tool error starting with `not_applied` |

The error's `details` hold the full `SettingChange` (`robot_id`, `setting`, `requested`,
`outcome: "not_applied"`, `actual`). Retrying rarely helps: a setting the robot drops is
usually one its firmware doesn't support.

Other errors: an unknown setting or a value outside the table is `invalid_request` (the
details list the valid values); a setting the model lacks is `not_supported`; a robot that
isn't connected is `robot_offline`.

## Schedule hold vs. app schedules

Robots store their own weekly schedules and run them even when the kit is the only thing you
use. On the i5 those entries trigger cloud routines; you can see them in `roomba-kit state` as
`schedules`. `schedule_hold` pauses every schedule stored on the robot without deleting it, so
a robot can't start on an old vendor routine alongside your own automations. Set it back to
`false` to resume them. The kit doesn't edit schedules (they are read-only in V1), and the
iRobot app can still start the robot directly from the cloud. Whether a hold also stops the
i5's schedule-triggered team routines is not yet verified on hardware.

## How `verify` works

`verify` closes `unverified` capabilities by exercising them on the real robot (Constitution
VIII). It checks only capabilities the model declares; naming another one is `not_supported`.

```bash
roomba-kit verify Vacuum setting.passes setting.edge_clean      # no motion
roomba-kit verify Mop --allow-motion --write-report             # asks before moving
```

HTTP: `POST /v1/robots/{robot}/verify` with `{"capabilities": [...], "allow_motion": false}`.
MCP: the `verify` tool. Leave `capabilities` out to check every capability the model declares;
an empty list is `invalid_request`.

| Capability | What it does | Needs `allow_motion` |
|---|---|---|
| `setting.*` | Writes a different value, reads it back, restores the robot's original keys, reads that back | no |
| `find` | Plays the find sound and waits for the robot's echo | no |
| `state.*` | Checks the robot reports that telemetry (`state.clean_base` needs a Clean Base reporting its state) | no |
| `live_position` | Passes only with a real fix. A docked robot has none, so it's retried while cleaning when the motion sequence runs; otherwise skipped | no |
| `cloud.rooms`, `cloud.history` | Fetches maps or mission history; an empty answer fails (skipped without the cloud) | no |
| `cloud.coverage`, `cloud.timeline` | Skipped: they need a mission; check them during or after a run | — |
| `evac` | Empties the bin, then waits for the robot to report docked again; the robot must be docked | yes |
| `clean`, `pause`, `resume`, `stop`, `dock` | From the dock: clean, then pause/resume/stop as named, then always dock and wait for docked. `dock` only counts when the robot was off the dock | yes |
| `clean_rooms` | Skipped: not scripted; run a room clean by hand | — |
| `reboot` | Reboots and waits for the robot to reconnect (up to 3 minutes); the robot must be docked | yes |
| `train` | Starts a mapping run from the dock. **Only when named explicitly**, because it can re-create the map and change room IDs | yes |

Each result is `pass`, `fail` or `skipped`, with evidence. A restore is always attempted once
a write was, and a setting whose restore fails or errors is reported as `fail` with
`RESTORE FAILED — set it by hand` first. In the motion sequence a step that doesn't pass skips
the ones after it, but a robot that left the dock is still sent home. If the request is
cancelled mid-run (a closed client, a stopped CLI), the setting being checked is still restored
and the robot is still docked. After evac or the motion sequence the kit waits up to 3 minutes
for the robot to report docked; until it does, `evac`, `reboot` and `train` are skipped.

Every run is stored in the service database and returned with a markdown report. The CLI's
`--write-report` appends it to `docs/verification/<profile>.md`; run it from the repository
root. Each report has a stable anchor, `run-YYYYMMDD-HHMMSS` (UTC), so a profile cites a passing
run as:

```python
Verification.verified(run="docs/verification/roomba_i5.md#run-20261108-213000")
```

The report contains the model, SKU and firmware, never the robot's ID, name or address, so it
is safe to commit.

Before a motion run: the robot is docked and charged, nothing else holds its local connection
(close the iRobot app), and there's room for it to drive.
