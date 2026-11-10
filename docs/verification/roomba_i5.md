# Verification: Roomba i5/i5+

Hardware: Roomba i5+ (`i5G5020`, firmware `daredevil+2.6.0`) with Clean Base, verified on
2026-11-09 (hardware session 1, "HW-1") with `roomba-kit verify … --write-report`. The
profile (`src/roomba_kit/profiles/roomba_i5.py`) cites the run anchors below. Run reports
follow the summary, oldest first; they contain no robot IDs, names or addresses.

## Summary

| Capability | Status | Evidence |
|---|---|---|
| `clean`, `pause`, `resume`, `stop` | ✅ verified | [run-20261109-185420](#run-20261109-185420): clean still cleaning 30 s later; pause 1.4 s; resume 1.0 s; stop 1.0 s |
| `dock` | ✅ verified | [run-20261109-211438](#run-20261109-211438): from cleaning (the kit pauses first), confirmed in 0.6 s |
| `find` | ✅ verified | [run-20261109-141750](#run-20261109-141750) |
| `evac` | ✅ verified | [run-20261109-142228](#run-20261109-142228): `evac` row only (that run's `clean` is superseded, see below) |
| `reboot` | ✅ verified | [run-20261109-194131](#run-20261109-194131): offline and back in 2.5 min (sent as `reset`) |
| `clean_rooms` | ✅ verified | [Laundry Room clean](#hw1-laundry-room-clean) |
| `state.bin`, `state.clean_base` | ✅ verified | [run-20261109-141750](#run-20261109-141750) |
| `setting.passes`, `edge_clean`, `always_finish`, `child_lock`, `eco_charge`, `schedule_hold` | ✅ verified | [run-20261109-141750](#run-20261109-141750): written, read back, restored. `schedule_hold`: write/read-back only; whether a held robot-stored schedule stays silent was not observed |
| `cloud.rooms`, `cloud.history` | ✅ verified | [run-20261109-141750](#run-20261109-141750) |
| `cloud.coverage` | ✅ verified | [Cloud coverage](#hw1-cloud-coverage) |
| `cloud.timeline` | ✅ verified | [Laundry Room clean](#hw1-laundry-room-clean) |
| `train` | ⚪ unverified | Not run: needs the owner's explicit OK (it can re-create the map and change room IDs) |
| `live_position`, `setting.carpet_boost` | — not supported | `live_position` verified absent in discovery (368 silent requests) |

Runs that failed or were superseded are kept below as the record. Each one drove a kit
change logged in `specs/001-roomba-kit-v1/decisions.md`:

| Run | What happened | Fix |
|---|---|---|
| run-20261109-140359 | No state after HOME-button pairing (session connected, robot sent nothing) | #64 retry a silent session |
| run-20261109-142228 | `clean` "passed" on a 1 s cleaning blip; mission aborted right after an evac (not-ready 68) | #66 sustained clean, wait for not-ready |
| run-20261109-170859 | `pause` 9 s into a clean ended the mission | #70 clean 30 s before pausing |
| run-20261109-185249, -190707 | Commands sent on a half-dead link | #73, #75 detect dead links and really reconnect |
| run-20261109-190834 | `dock` while cleaning was ignored | #72 pause before dock |
| run-20261109-191625 | `reboot` ignored (wrong firmware command) | #71 send `reset` |

<a id="hw1-laundry-room-clean"></a>
## HW-1: Laundry Room clean (`clean_rooms`, `cloud.timeline`)

`roomba-kit clean Patty --room "Laundry Room"` at 14:28 UTC: confirmed `cleaning` in 7.7 s
(room 7, current map). `missions wait` returned the terminal mission: outcome `completed`,
`rooms_requested ["7"]`, initiator `kit`, 2,313 s. Cloud timeline events arrived over the
shared cloud session within seconds: `start` (14:28:23), `travel → room 7` (14:28:49),
`room 7` (14:29:46). Note: the robot's local link went quiet during the run (no state for
30 min); the kit now reconnects silent links mid-mission (#68).

<a id="hw1-cloud-coverage"></a>
## HW-1: Cloud coverage (`cloud.coverage`)

`roomba-kit coverage Patty <laundry mission>` after the run: 491 coverage points and 244
clutter points from the mission's own map version. Missions not yet published return
`cloud_unavailable(not_yet_published)`.

## Run reports

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

<a id="run-20261109-170859"></a>

### 2026-11-09 17:08:59 UTC · `roomba_i5`

- Model: Roomba i5/i5+, SKU `i5G5020`, firmware `daredevil+2.6.0+daredevil-release+163`
- Kit 0.1.0; motion allowed: yes
- 1 pass, 1 fail, 3 skipped
- Cite as `docs/verification/roomba_i5.md#run-20261109-170859`

| Capability | Result | Evidence |
|---|---|---|
| `clean` | pass | confirmed after 7758 ms (activity cleaning); still cleaning 8 s later |
| `pause` | fail | unconfirmed after 10001 ms (activity unknown) |
| `resume` | skipped | not run: pause didn't pass |
| `stop` | skipped | not run: pause didn't pass |
| `dock` | skipped | not checked: pause didn't pass; sent home anyway: confirmed after 860 ms (activity returning) |

<a id="run-20261109-185249"></a>

### 2026-11-09 18:52:49 UTC · `roomba_i5`

- Model: Roomba i5/i5+, SKU `i5G5020`, firmware `daredevil+2.6.0+daredevil-release+163`
- Kit 0.1.0; motion allowed: yes
- 0 pass, 1 fail, 4 skipped
- Cite as `docs/verification/roomba_i5.md#run-20261109-185249`

| Capability | Result | Evidence |
|---|---|---|
| `clean` | fail | unconfirmed after 20001 ms (activity docked) |
| `pause` | skipped | not run: clean didn't pass |
| `resume` | skipped | not run: clean didn't pass |
| `stop` | skipped | not run: clean didn't pass |
| `dock` | skipped | not checked: clean didn't pass |

<a id="run-20261109-185420"></a>

### 2026-11-09 18:54:20 UTC · `roomba_i5`

- Model: Roomba i5/i5+, SKU `i5G5020`, firmware `daredevil+2.6.0+daredevil-release+163`
- Kit 0.1.0; motion allowed: yes
- 4 pass, 1 fail, 0 skipped
- Cite as `docs/verification/roomba_i5.md#run-20261109-185420`

| Capability | Result | Evidence |
|---|---|---|
| `clean` | pass | confirmed after 7817 ms (activity cleaning); still cleaning 30 s later |
| `pause` | pass | confirmed after 1436 ms (activity paused) |
| `resume` | pass | confirmed after 1029 ms (activity cleaning) |
| `stop` | pass | confirmed after 1015 ms (activity unknown) |
| `dock` | fail | unconfirmed after 10001 ms (activity unknown); WARNING: the robot wasn't back on its dock within 180 s |

<a id="run-20261109-190707"></a>

### 2026-11-09 19:07:07 UTC · `roomba_i5`

- Model: Roomba i5/i5+, SKU `i5G5020`, firmware `daredevil+2.6.0+daredevil-release+163`
- Kit 0.1.0; motion allowed: yes
- 0 pass, 0 fail, 1 skipped
- Cite as `docs/verification/roomba_i5.md#run-20261109-190707`

| Capability | Result | Evidence |
|---|---|---|
| `dock` | skipped | not checked: clean didn't pass |

<a id="run-20261109-190834"></a>

### 2026-11-09 19:08:34 UTC · `roomba_i5`

- Model: Roomba i5/i5+, SKU `i5G5020`, firmware `daredevil+2.6.0+daredevil-release+163`
- Kit 0.1.0; motion allowed: yes
- 0 pass, 1 fail, 0 skipped
- Cite as `docs/verification/roomba_i5.md#run-20261109-190834`

| Capability | Result | Evidence |
|---|---|---|
| `dock` | fail | unconfirmed after 10001 ms (activity cleaning); WARNING: the robot wasn't back on its dock within 180 s |

<a id="run-20261109-191443"></a>

### 2026-11-09 19:14:43 UTC · `roomba_i5`

- Model: Roomba i5/i5+, SKU `i5G5020`, firmware `daredevil+2.6.0+daredevil-release+163`
- Kit 0.1.0; motion allowed: yes
- 0 pass, 0 fail, 1 skipped
- Cite as `docs/verification/roomba_i5.md#run-20261109-191443`

| Capability | Result | Evidence |
|---|---|---|
| `reboot` | skipped | the robot must be docked; it is cleaning |

<a id="run-20261109-191625"></a>

### 2026-11-09 19:16:25 UTC · `roomba_i5`

- Model: Roomba i5/i5+, SKU `i5G5020`, firmware `daredevil+2.6.0+daredevil-release+163`
- Kit 0.1.0; motion allowed: yes
- 0 pass, 1 fail, 0 skipped
- Cite as `docs/verification/roomba_i5.md#run-20261109-191625`

| Capability | Result | Evidence |
|---|---|---|
| `reboot` | fail | unconfirmed after 180001 ms (activity docked) |

<a id="run-20261109-194131"></a>

### 2026-11-09 19:41:31 UTC · `roomba_i5`

- Model: Roomba i5/i5+, SKU `i5G5020`, firmware `daredevil+2.6.0+daredevil-release+163`
- Kit 0.1.0; motion allowed: yes
- 1 pass, 0 fail, 0 skipped
- Cite as `docs/verification/roomba_i5.md#run-20261109-194131`

| Capability | Result | Evidence |
|---|---|---|
| `reboot` | pass | confirmed after 152841 ms (activity docked) |

<a id="run-20261109-211438"></a>

### 2026-11-09 21:14:38 UTC · `roomba_i5`

- Model: Roomba i5/i5+, SKU `i5G5020`, firmware `daredevil+2.6.0+daredevil-release+163`
- Kit 0.1.0; motion allowed: yes
- 1 pass, 0 fail, 0 skipped
- Cite as `docs/verification/roomba_i5.md#run-20261109-211438`

| Capability | Result | Evidence |
|---|---|---|
| `dock` | pass | confirmed after 571 ms (activity returning) |
