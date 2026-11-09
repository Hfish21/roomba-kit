# Verification: Braava jet m6

_This page is written in a later task; see `specs/001-roomba-kit-v1/tasks.md`._

<a id="run-20261109-141806"></a>

### 2026-11-09 14:18:06 UTC · `braava_m6`

- Model: Braava jet m6, SKU `m611020`, firmware `sanmarino+22.29.6+2022-12-01-82f3372a65c+Firmware-Build+2321`
- Kit 0.1.0; motion allowed: no
- 13 pass, 0 fail, 11 skipped
- Cite as `docs/verification/braava_m6.md#run-20261109-141806`

| Capability | Result | Evidence |
|---|---|---|
| `setting.passes` | pass | auto → one: applied (read back one); restored → auto: applied (read back auto) |
| `setting.edge_clean` | pass | True → False: applied (read back False); restored → True: applied (read back True) |
| `setting.child_lock` | pass | False → True: applied (read back True); restored → False: applied (read back False) |
| `setting.eco_charge` | pass | False → True: applied (read back True); restored → False: applied (read back False) |
| `setting.schedule_hold` | pass | False → True: applied (read back True); restored → False: applied (read back False) |
| `setting.pad_wetness` | pass | 3 → 1: applied (read back 1); restored → 3: applied (read back 3) |
| `setting.spray_overlap` | pass | standard → deep: applied (read back deep); restored → standard: applied (read back standard) |
| `find` | pass | confirmed after 204 ms (activity docked) |
| `state.pad` | pass | reported {"detected": "reusableWet"} |
| `state.tank` | pass | reported {"present": true, "level_pct": 100} |
| `state.lid` | pass | reported false |
| `live_position` | skipped | no fix (robot docked or idle): rerun with allow_motion and clean |
| `cloud.rooms` | pass | the cloud returned 1 maps |
| `cloud.history` | pass | the cloud returned 8 missions |
| `cloud.coverage` | skipped | not scripted: needs a mission; check it during or after a run |
| `cloud.timeline` | skipped | not scripted: needs a mission; check it during or after a run |
| `clean` | skipped | moves the robot: rerun with allow_motion |
| `pause` | skipped | moves the robot: rerun with allow_motion |
| `resume` | skipped | moves the robot: rerun with allow_motion |
| `stop` | skipped | moves the robot: rerun with allow_motion |
| `dock` | skipped | moves the robot: rerun with allow_motion |
| `clean_rooms` | skipped | acts on the robot: rerun with allow_motion |
| `reboot` | skipped | acts on the robot: rerun with allow_motion |
| `train` | skipped | runs only when named explicitly: it can re-create the map |
