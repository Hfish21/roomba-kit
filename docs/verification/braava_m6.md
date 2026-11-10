# Verification: Braava jet m6

Hardware: Braava jet m6 (`m611020`, firmware `sanmarino+22.29.6`), verified on 2026-11-09
(hardware session 1, "HW-1") with `roomba-kit verify … --write-report`, plus a room-clean
retest on 2026-11-10 ("HW-2"). The profile
(`src/roomba_kit/profiles/braava_m6.py`) cites the run anchors below. Run reports follow
the summary, oldest first; they contain no robot IDs, names or addresses.

## Summary

| Capability | Status | Evidence |
|---|---|---|
| `clean`, `pause`, `resume`, `stop` | ✅ verified | [run-20261109-210213](#run-20261109-210213): clean still cleaning 30 s later; pause 1.1 s; resume 0.5 s; stop 5.8 s |
| `live_position` | ✅ verified | [run-20261109-210213](#run-20261109-210213): fix at (−0.79, −1.60) m while mopping |
| `dock` | ✅ verified | [run-20261109-214546](#run-20261109-214546): from mopping (the kit pauses first), 0.5 s |
| `reboot` | ✅ verified | [run-20261109-214817](#run-20261109-214817): back in 96 s (sent as `reset`) |
| `find` | ✅ verified | [run-20261109-141806](#run-20261109-141806) |
| `state.pad`, `state.tank`, `state.lid` | ✅ verified | [run-20261109-141806](#run-20261109-141806); lid open/close also seen live during the tank fault below |
| `setting.passes`, `edge_clean`, `child_lock`, `eco_charge`, `schedule_hold`, `pad_wetness`, `spray_overlap` | ✅ verified | [run-20261109-141806](#run-20261109-141806): written, read back, restored |
| `cloud.rooms`, `cloud.history` | ✅ verified | [run-20261109-141806](#run-20261109-141806) |
| `cloud.coverage` | ✅ verified | [Cloud coverage](#hw1-cloud-coverage) |
| `clean_rooms` | ⚪ unverified | [Kitchen clean](#hw2-kitchen-room-clean): the robot cleaned the requested room, but the kit reported `unconfirmed` (she started ~57 s after the command, past the 20 s window) and the run was cut short (`cancelled`, not `completed`) |
| `cloud.timeline` | ✅ verified | [Kitchen clean](#hw2-kitchen-room-clean): `start`, `travel → room 1`, `room 1`, later `travel → dock`, live during the mission |
| `train` | ⚪ unverified | Not run: needs the owner's explicit OK |
| `evac`, `state.bin`, `state.clean_base`, `setting.always_finish`, `setting.carpet_boost` | — not supported | No Clean Base or bin |

Model behaviour the kit now handles (decisions #76, #77): `dock` while mopping is ignored
until paused; pause/stop/dock can land up to ~15 s after the echo (30 s windows); `dock`
right after `stop` is ignored for 30–70 s (the kit re-sends it).

<a id="hw1-cloud-coverage"></a>
## HW-1: Cloud coverage (`cloud.coverage`)

`roomba-kit coverage <the mop> <mission>` for the day's missions: one returned 2,258 coverage
points; the two most recent returned `cloud_unavailable(not_yet_published)` as documented.

<a id="hw1-tank-fault"></a>
## HW-1: Water tank not detected (blocked `clean_rooms`, `cloud.timeline`; resolved in HW-2)

After the day's mopping the m6 reported not-ready 31 ("water tank level low") with
`tank.present=false, level 0`. The owner refilled and reseated the tank several times and a
remote `reboot` was sent; the robot never registered the tank, while lid open/close showed up
live each time (so the kit was reading fresh state). A manual start on the robot failed the
same way, so this is a robot-side tank-detection fault (inferred: sensor or contacts). Its
first room clean attempt also correctly hit `map_changed`: the m6 saved a new map version
(`261109T213701`) during the session, and rooms were re-imported.

**Resolved (HW-2, 2026-11-10).** After the owner worked on the robot, not-ready 31 cleared at
01:43:36 UTC (briefly replaced by not-ready 34, "no mop pad attached", while the pad was
refitted). By 01:49:23 the m6 reported `tank.present=true, level 100`, pad `reusableWet`, lid
closed and no not-ready, and the room clean below ran.

<a id="hw2-kitchen-room-clean"></a>
## HW-2: Kitchen clean (`clean_rooms`, `cloud.timeline`)

Map version `261109T213701`, rooms current (`stale: false`); Kitchen is room 1.

- **First attempt, 01:50:29 UTC:** `roomba-kit clean <the mop> --room Kitchen` → `unconfirmed`
  after 20 s, and the robot never echoed it (`last_command` unchanged). It was docked with an
  owner button mission still open (started 01:45, paused 01:47) that the kit hadn't seen
  start, because the mop's local link was silent 01:44:57–01:49:21. Inferred: the m6 ignores
  `start` while a paused cycle is open. `stop` closed it (confirmed, 1.5 s; no motion).
- **Second attempt, 01:51:42:** echoed at once as `clean_rooms`, room `1`, but the mop raised
  not-ready 39 ("saving the map") then 68, and only started cleaning at 01:52:39, ~57 s after
  the command, with no re-send. The kit returned `unconfirmed` (20 s window). The mission
  recorded `rooms_requested ["1"]`, initiator `kit`.
- **Cloud timeline,** over the shared cloud session within seconds of each step: `start`
  (01:52:41), `travel → room 1` (01:52:53, again 01:53:05), `room 1` (01:53:41), plus
  `tentativeLoc`, `plan` and `reloc` (shown as `other`). The previous mission's `fin` arrived
  at 01:51:39.
- **End of the run:** the developer Mac slept 02:09–02:26 UTC, freezing the service mid-run;
  after it woke, another client paused and docked the mop at 02:28 (cloud: `pause`,
  `travel → dock`, `traversal` room 0). The kit raised `alert.dock_search_timeout` after
  5 min of `returning`; she reached the dock at 02:35:49 (~7.5 min) and went `charging`.
  `missions wait` returned outcome `cancelled`, `rooms_requested ["1"]`, initiator `kit`,
  2,603 s (including the 17 min the Mac slept). She didn't finish and dock on her own.

`cloud.timeline` passes: real events for the kit's own mission, rooms and destinations mapped.
`clean_rooms` stays unverified: the room was cleaned, but the kit's own result was
`unconfirmed` and the mission ended `cancelled`, not `completed`. Before a retest: widen the m6's `clean` /
`clean_rooms` window (or hold confirmation open through transient not-ready 39/68).

## Run reports

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

<a id="run-20261109-205447"></a>

### 2026-11-09 20:54:47 UTC · `braava_m6`

- Model: Braava jet m6, SKU `m611020`, firmware `sanmarino+22.29.6+2022-12-01-82f3372a65c+Firmware-Build+2321`
- Kit 0.1.0; motion allowed: yes
- 0 pass, 2 fail, 4 skipped
- Cite as `docs/verification/braava_m6.md#run-20261109-205447`

| Capability | Result | Evidence |
|---|---|---|
| `live_position` | fail | no answer within 5 s |
| `clean` | fail | unconfirmed after 20001 ms (activity docked) |
| `pause` | skipped | not run: clean didn't pass |
| `resume` | skipped | not run: clean didn't pass |
| `stop` | skipped | not run: clean didn't pass |
| `dock` | skipped | not checked: clean didn't pass |

<a id="run-20261109-205527"></a>

### 2026-11-09 20:55:27 UTC · `braava_m6`

- Model: Braava jet m6, SKU `m611020`, firmware `sanmarino+22.29.6+2022-12-01-82f3372a65c+Firmware-Build+2321`
- Kit 0.1.0; motion allowed: yes
- 0 pass, 2 fail, 4 skipped
- Cite as `docs/verification/braava_m6.md#run-20261109-205527`

| Capability | Result | Evidence |
|---|---|---|
| `live_position` | fail | no answer within 5 s |
| `clean` | fail | unconfirmed after 20001 ms (activity docked) |
| `pause` | skipped | not run: clean didn't pass |
| `resume` | skipped | not run: clean didn't pass |
| `stop` | skipped | not run: clean didn't pass |
| `dock` | skipped | not checked: clean didn't pass |

<a id="run-20261109-210213"></a>

### 2026-11-09 21:02:13 UTC · `braava_m6`

- Model: Braava jet m6, SKU `m611020`, firmware `sanmarino+22.29.6+2022-12-01-82f3372a65c+Firmware-Build+2321`
- Kit 0.1.0; motion allowed: yes
- 5 pass, 1 fail, 0 skipped
- Cite as `docs/verification/braava_m6.md#run-20261109-210213`

| Capability | Result | Evidence |
|---|---|---|
| `live_position` | pass | fix at (-0.79, -1.60) m |
| `clean` | pass | confirmed after 629 ms (activity cleaning); still cleaning 30 s later |
| `pause` | pass | confirmed after 1069 ms (activity paused) |
| `resume` | pass | confirmed after 518 ms (activity cleaning) |
| `stop` | pass | confirmed after 5801 ms (activity unknown) |
| `dock` | fail | unconfirmed after 10001 ms (activity unknown); WARNING: the robot wasn't back on its dock within 180 s |

<a id="run-20261109-210732"></a>

### 2026-11-09 21:07:32 UTC · `braava_m6`

- Model: Braava jet m6, SKU `m611020`, firmware `sanmarino+22.29.6+2022-12-01-82f3372a65c+Firmware-Build+2321`
- Kit 0.1.0; motion allowed: yes
- 0 pass, 1 fail, 0 skipped
- Cite as `docs/verification/braava_m6.md#run-20261109-210732`

| Capability | Result | Evidence |
|---|---|---|
| `dock` | fail | unconfirmed after 10001 ms (activity cleaning); WARNING: the robot wasn't back on its dock within 180 s |

<a id="run-20261109-214546"></a>

### 2026-11-09 21:45:46 UTC · `braava_m6`

- Model: Braava jet m6, SKU `m611020`, firmware `sanmarino+22.29.6+2022-12-01-82f3372a65c+Firmware-Build+2321`
- Kit 0.1.0; motion allowed: yes
- 1 pass, 0 fail, 0 skipped
- Cite as `docs/verification/braava_m6.md#run-20261109-214546`

| Capability | Result | Evidence |
|---|---|---|
| `dock` | pass | confirmed after 519 ms (activity returning) |

<a id="run-20261109-214817"></a>

### 2026-11-09 21:48:17 UTC · `braava_m6`

- Model: Braava jet m6, SKU `m611020`, firmware `sanmarino+22.29.6+2022-12-01-82f3372a65c+Firmware-Build+2321`
- Kit 0.1.0; motion allowed: yes
- 1 pass, 0 fail, 0 skipped
- Cite as `docs/verification/braava_m6.md#run-20261109-214817`

| Capability | Result | Evidence |
|---|---|---|
| `reboot` | pass | confirmed after 96290 ms (activity charging) |
