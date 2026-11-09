# Hardware captures (scrubbed)

These are real payloads captured on 2026-11-08 from a **Roomba i5+** (`i5G5020`, firmware
`daredevil+2.6.0`) and a **Braava jet m6** (`m611020`, firmware `sanmarino+22.29.6`), over the
local MQTT connection and the iRobot cloud. They are the source material for the
FakeTransport/FakeCloud fixtures (tasks.md T020) and for profile tests.

## Scrubbing

- **Robot IDs.** The i5 is `00000000000000000000000000000001` and the m6 is
  `00000000000000000000000000000002`. Other 32-hex IDs become `FFFF…000N`.
- **Removed keys**, at any depth: `passwordHash`, `netinfo`, `wlcfg`, `svcEndpoints`,
  `batInfo`, `hwDbgr`, `mac`, `wlan0HwAddr`, `navSerialNo`, `mobBlid`, `cloudEnv`, `country`,
  `timezone`, `tz`, `langs2`, plus any key containing `password`, `ssid`, `serial`, `token`,
  `email` or `username`.
- **IPs, MACs and emails** are replaced with documentation ranges. Robot display names become
  `Vacuum` / `Mop`.
- **Verified** after scrubbing: no robot password, account credential, real ID, IP or MAC
  remains.
- **Kept as-is.** Room names are generic room types (e.g. "Laundry Room"). Map and floor-plan
  geometry are kept because floor-plan and coverage tests need them.

## Files

| File | What it is |
|---|---|
| `i5_state.json` / `m6_state.json` | Full merged local shadow (`state.reported`), docked |
| `i5_local_messages.jsonl` / `m6_local_messages.jsonl` | Raw local message stream: probe, `find`, start→pause→resume→dock, evac |
| `i5_room_clean_run.jsonl` | Laundry-room clean: phase transitions and the rrtp position probe summary (0 fixes, 368 silent) |
| `m6_positions.json` | rrtp position fixes `[elapsed_s, x_m, y_m, theta_rad]` during an m6 run |
| `i5_pmaps.json` / `m6_pmaps.json` | Cloud stored maps, with regions (room IDs, names, types) |
| `i5_mission_history.json` / `m6_mission_history.json` | Cloud mission history, including timelines (`finEvents`) |
| `i5_mission_umf.json`, `i5_mission_umf_small.json`, `m6_mission_umf.json` | Per-mission UMF: `coverage`, `dirt`, `clutter`, `coverage_poly` layers |
| `i5_floorplan_umf.json` / `m6_floorplan_umf.json` | Stored-map UMF (room outlines) |
| `i5_cloud_shadow.json` | Classic shadow read over cloud MQTT |
| `i5_cloud_live_run.jsonl` | Cloud MQTT during a room clean: mission timeline reports, shadow documents, cloud rrtp request echoes (no reply) |

Regenerate only from new hardware captures. Never commit unscrubbed payloads.
