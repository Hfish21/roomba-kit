# Verification: Roomba i5/i5+

_This page is written in a later task; see `specs/001-roomba-kit-v1/tasks.md`._

<a id="run-20261109-140359"></a>

### 2026-11-09 14:03:59 UTC · `roomba_i5`

- Model: Roomba i5/i5+, SKU `i5G5020`, firmware `daredevil+2.6.0+daredevil-release+163`
- Kit 0.1.0; motion allowed: no
- 2 pass, 9 fail, 11 skipped
- Cite as `docs/verification/roomba_i5.md#run-20261109-140359`

| Capability | Result | Evidence |
|---|---|---|
| `setting.passes` | fail | the robot doesn't report passes |
| `setting.edge_clean` | fail | the robot doesn't report edge_clean |
| `setting.always_finish` | fail | the robot doesn't report always_finish |
| `setting.child_lock` | fail | the robot doesn't report child_lock |
| `setting.eco_charge` | fail | the robot doesn't report eco_charge |
| `setting.schedule_hold` | fail | the robot doesn't report schedule_hold |
| `find` | fail | unconfirmed after 5001 ms (activity unknown) |
| `state.bin` | fail | reported null |
| `state.clean_base` | fail | reported null |
| `cloud.rooms` | pass | the cloud returned 1 maps |
| `cloud.history` | pass | the cloud returned 30 missions |
| `cloud.coverage` | skipped | not scripted: needs a mission; check it during or after a run |
| `cloud.timeline` | skipped | not scripted: needs a mission; check it during or after a run |
| `evac` | skipped | acts on the robot: rerun with allow_motion |
| `clean` | skipped | moves the robot: rerun with allow_motion |
| `pause` | skipped | moves the robot: rerun with allow_motion |
| `resume` | skipped | moves the robot: rerun with allow_motion |
| `stop` | skipped | moves the robot: rerun with allow_motion |
| `dock` | skipped | moves the robot: rerun with allow_motion |
| `clean_rooms` | skipped | acts on the robot: rerun with allow_motion |
| `reboot` | skipped | acts on the robot: rerun with allow_motion |
| `train` | skipped | runs only when named explicitly: it can re-create the map |

<a id="run-20261109-141750"></a>

### 2026-11-09 14:17:50 UTC · `roomba_i5`

- Model: Roomba i5/i5+, SKU `i5G5020`, firmware `daredevil+2.6.0+daredevil-release+163`
- Kit 0.1.0; motion allowed: no
- 11 pass, 0 fail, 11 skipped
- Cite as `docs/verification/roomba_i5.md#run-20261109-141750`

| Capability | Result | Evidence |
|---|---|---|
| `setting.passes` | pass | one → two: applied (read back two); restored → one: applied (read back one) |
| `setting.edge_clean` | pass | True → False: applied (read back False); restored → True: applied (read back True) |
| `setting.always_finish` | pass | False → True: applied (read back True); restored → False: applied (read back False) |
| `setting.child_lock` | pass | False → True: applied (read back True); restored → False: applied (read back False) |
| `setting.eco_charge` | pass | False → True: applied (read back True); restored → False: applied (read back False) |
| `setting.schedule_hold` | pass | False → True: applied (read back True); restored → False: applied (read back False) |
| `find` | pass | confirmed after 819 ms (activity docked) |
| `state.bin` | pass | reported {"present": true, "full": false} |
| `state.clean_base` | pass | reported {"present": true, "state": 301} |
| `cloud.rooms` | pass | the cloud returned 1 maps |
| `cloud.history` | pass | the cloud returned 31 missions |
| `cloud.coverage` | skipped | not scripted: needs a mission; check it during or after a run |
| `cloud.timeline` | skipped | not scripted: needs a mission; check it during or after a run |
| `evac` | skipped | acts on the robot: rerun with allow_motion |
| `clean` | skipped | moves the robot: rerun with allow_motion |
| `pause` | skipped | moves the robot: rerun with allow_motion |
| `resume` | skipped | moves the robot: rerun with allow_motion |
| `stop` | skipped | moves the robot: rerun with allow_motion |
| `dock` | skipped | moves the robot: rerun with allow_motion |
| `clean_rooms` | skipped | acts on the robot: rerun with allow_motion |
| `reboot` | skipped | acts on the robot: rerun with allow_motion |
| `train` | skipped | runs only when named explicitly: it can re-create the map |

<a id="run-20261109-142228"></a>

### 2026-11-09 14:22:28 UTC · `roomba_i5`

- Model: Roomba i5/i5+, SKU `i5G5020`, firmware `daredevil+2.6.0+daredevil-release+163`
- Kit 0.1.0; motion allowed: yes
- 2 pass, 1 fail, 3 skipped
- Cite as `docs/verification/roomba_i5.md#run-20261109-142228`

| Capability | Result | Evidence |
|---|---|---|
| `evac` | pass | confirmed after 2939 ms (activity emptying_bin) |
| `clean` | pass | confirmed after 3832 ms (activity cleaning) |
| `pause` | fail | unconfirmed after 10001 ms (activity docked) |
| `resume` | skipped | not run: pause didn't pass |
| `stop` | skipped | not run: pause didn't pass |
| `dock` | skipped | not checked: pause didn't pass |
