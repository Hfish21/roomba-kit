# Discovery record: controlling iRobot robots without the iRobot app (2026-11-08)

**Conclusion:** Both robots (Roomba i5+ and Braava jet m6) are fully controllable over the home
LAN without iRobot's app or cloud, using the robot's local MQTT interface. iRobot's unofficial
cloud adds room names, floor plans, history, coverage paths and a live room-level timeline. Only
the m6 reports live x/y position; the i5+ firmware never does, by any route we found. Custom
firmware is not viable (secure boot). Everything below was **verified on hardware** unless it is
marked *(inferred)* or *(third-party)*.

Session transcript: `recall --show b10c0fbc`. Test fixtures built from these captures:
`tests/fixtures/captures/`.

---

## 1. Hardware

| | Vacuum | Mop |
|---|---|---|
| Model / SKU | Roomba i5+ / `i5G5020` | Braava jet m6 / `m611020` |
| Firmware | `daredevil+2.6.0` (final release for i1–i5; frozen) | `sanmarino+22.29.6` (Dec 2022) |
| Dock | Clean Base `ADJ-N1` (auto-empty) | Standard dock |
| Navigation | Gyro/IMU (BMI088) + wheel odometry + floor-tracking sensor. **No camera** (`nGoodLmrks=0` over 678 h) | Camera vSLAM |
| Secure boot | Section present | `enforce: 2`, efuse set, so enforced |
| Stored maps | 1 (`FbKwAujERSazAALZRD_rEg`, version `231231T204559`) | 1 (`5brjBHI-RtKr1Yt7JzFjDw`, version `260128T162258`) |

## 2. Local control (no cloud)

**Discovery**
- UDP broadcast `irobotmcs` → port 5678. Each robot replies with JSON (`robotid`, `robotname`,
  `ip`, `mac`, `sw`, `sku`, `cap`).
- ⚠ This Mac's default route is a VPN (`utun3`), so broadcasting to `255.255.255.255` fails with
  `EADDRNOTAVAIL`. Use the subnet broadcast `192.168.1.255`.

**Password**
- Put the robot on its dock and hold HOME about 2 s until it chimes.
- Within about 30 s, open TLS to `:8883` and send `f005efcc3b2900`. The password is reply
  bytes 7 onward.
- Worked on both robots; the password is 30 chars. The reply `f005efcc3b2903` means "cloud
  only" *(third-party)*.

**Transport**
- MQTT 3.1.1 over TLS on 8883. The self-signed cert is unverified, and OpenSSL 3 needs
  `DEFAULT:!DH` plus legacy renegotiation.
- Username and client ID are the BLID; the password is the robot password.
- **One local client at a time.** A second client evicts the first, and the iRobot app falls
  back to cloud while we hold the connection.

**Topics**
- Subscribe to `#` for shadow `state.reported` fragments.
- Commands are published to `cmd` as `{"command", "time", "initiator":"localApp", …params}`.
- Settings are published to `delta` as `{"state":{…}}`.
- Position requests go to `req`, with replies on `data`.

**Commands verified**, all echoed in `lastCommand` with `initiator: localApp`:

| Command | i5+ | m6 | Observed |
|---|---|---|---|
| `find` | ✅ | ✅ | Echo within ~1 s |
| `start` | ✅ | ✅ | `phase` charge→run in ~5 s |
| `pause` / `resume` | ✅ | ✅ | Usually <1 s. **The m6's second pause registered ~5 s late.** |
| `dock` (after pause) | ✅ | ✅ | `hmUsrDock` → docked in 35–40 s from close range |
| `evac` | ✅ | n/a | `cycle: evac` for ~16 s. A dock after a mission auto-evacs. |
| Room clean | ✅ (Laundry, rid 7) | untested | Params: `{pmap_id, user_pmapv_id, ordered:1, regions:[{region_id:"7", type:"rid"}]}`. Went straight to the room; cleaned for 16–17 min. |

**Phases seen:**
- `phase`: `charge`, `run`, `stop`, `hmUsrDock`, `hmPostMsn`, `evac`.
- `cycle`: `none`, `clean`, `evac`, `dock`.
- `notReady`: 39 and 68 appear transiently around docking.

**Settings keys**, read on hardware; writes untested:

| Robot | Keys |
|---|---|
| i5 | `binPause`, `openOnly`, `noAutoPasses`, `twoPass`, `carpetBoost`, `vacHigh`, `childLock`, `ecoCharge`, `schedHold`, `rankOverlap` |
| m6 | `padWetness {disposable, reusable}` (1–3), `rankOverlap` (67 standard / 85 deep / 25 extended), `detectedPad`, `tankLvl`, `tankPresent`, `lidOpen` |

*(Third-party, Roomba+)*: on an i3, `noAutoPasses` and `twoPass` must be written together,
and `carpetBoost` writes are silently dropped.

**Robot-stored schedules:**
- The i5 has 5 weekday 09:00 entries in `cleanSchedule2`. Each one triggers a cloud "team" routine
  (vacuum→mop), not a direct clean.
- The m6 has 1 entry, disabled.

## 3. Live position

| Route | i5+ | m6 |
|---|---|---|
| Local rrtp (`req` `{"reqId","reqType":"current","conType":"local"}` → `data`) | **No reply ever**: 368 requests over 16 min of active cleaning | ✅ Fix about every 2 s while cleaning (47 fixes). Docked → answers "no fix". |
| Cloud rrtp on `{irbt}/things/{blid}/req` | **Forbidden** by AWS policy; the session is dropped | not tried |
| Cloud rrtp on `{irbt}/things/{blid}/mission/rrtp/request` | Broker ACKs it, but the robot never replies | not tried |
| Cloud `livemap/update` | Silent (V4/Prime robots only) | not tried |
| Cloud mission timeline | ✅ Room-level, seconds of latency (see §4) | untested |
| After-run coverage (cloud UMF) | ✅ Thousands of points | ✅ |

Not pursued, judged not worth it:
- **Packet capture:** the traffic is TLS, so it yields cadence and endpoints, not coordinates.
- **Firmware RE:** even if the code exists, secure boot blocks reaching it.

## 4. iRobot cloud (unofficial; via `roombapy-prime` 0.8.3)

**Login:** `CloudAccount.login(session, user, pw, "US", app_id="IOS-<uuid>")`. It returns every
robot on the account, including each robot's local password.

**REST** (`account.classic_rest()`):

| Call | Returns |
|---|---|
| `get_pmaps(blid)` | Rooms: `active_pmapv_details.regions[]` with `id`, `name`, `region_type` |
| `get_mission_history(blid)` | About 30 most recent missions, with `timeline.finEvents` |
| `get_pmap_umf(blid, pmap_id, version)` | Floor plan, or per-mission coverage using the mission's own `pmaps_info` IDs |

**Rooms:**
- i5: 1 Living Room (open plan with kitchen and dining), 3 Den, 4 Office, 5 Primary Bathroom,
  6 Primary Bedroom, 7 Laundry Room, 8 Guest Bathroom. There is no rid 2.
- m6: 1 Kitchen; rids 0 and 2–5 are unnamed.

**Cloud MQTT:**
- `PrimeFactory.create_prime_robot(..., blid=, login_result=)` works for Classic robots, even
  though `CloudAccount.prime_robot` refuses them.
- Endpoint: AWS IoT over WebSocket. The irbt topic prefix was `v007-irbthbu`.
- The classic shadow is readable (71 keys, no pose).
- Live timeline topic: `{prefix}/things/{blid}/mission/timeline/report`. It carries:
  - `start`;
  - `travel {dest: room|dock, rid}`;
  - `room`;
  - `traversal {rid}`;
  - `evac`;
  - `fin`.

  It produced 50 messages in one 14-minute run, each arriving within seconds.
- Wildcard `{prefix}/things/{blid}/#` subscribe is accepted.

**Timing:** a mission does **not** appear in cloud history while running, and was still absent
about 2 minutes after docking. Coverage is after-the-fact only, with minutes of lag.

**Open risk:** two concurrent per-robot cloud MQTT sessions under one login may evict each other
(plan R12, task T076).

## 5. Behavioural findings (from robot counters and cloud history)

**Failure rates:**
- i5: 263 ok, 186 failed, 98 cancelled.
- m6: 22 ok, 134 failed of 179.

**Schedules:** the i5's last scheduled run was 2026-08-21 and the m6's was 2026-02-03, while the
schedules still showed as enabled. Every schedule runs through cloud team routines, which matches
the owner's "never runs on schedule" complaint.

**Docking is intermittent on the i5:**
- In history, docking almost always took 0–2 min until it failed on 2026-08-20 and 2026-08-21.
- Today's first run hunted for 70 min, crossing Living Room, Guest Bathroom, Laundry and Office,
  until the owner placed it (`done_raw: plcDoc`).
- The second identical run docked on its own in about 90 s.
- The likely cause is the dock's surroundings or IR line of sight *(inferred)*. The kit alerts
  on it (`dock_search_timeout`) rather than trying to fix it.

**Short runs:** many scheduled "ok" runs lasted 4–6 min and covered about 30 sq ft, likely
single-room routines *(inferred)*.

## 6. Dead ends (don't repeat)

| Idea | Why it's dead |
|---|---|
| Custom firmware / root | No public root for i/j/m-series. The m6 enforces secure boot. |
| Live i5 coordinates | Not exposed locally or via the cloud; see §3 |
| Matter | Not offered on the i5 or m6 (only on 2025+ models) *(third-party)* |
| Home Assistant as the platform | The owner wants owned software; HA libraries are used only as protocol evidence |

## 7. Environment gotchas on this machine

| Gotcha | What to do |
|---|---|
| VPN owns the default route | Use the subnet broadcast `192.168.1.255` for discovery |
| `SPECIFY_INIT_DIR=~/context` is set globally in `~/.claude/settings.json` | The repo's `.specify/scripts/python/common.py` is patched so the nearest `.specify/` wins. Re-apply after `specify init --force`. |
| The 1Password CLI intermittently fails reads | Testing credentials are in `~/scratch/roomba-kit/roomba.env` (0600; robot BLIDs and passwords plus iRobot login). Never commit them. |
| The auto-mode classifier blocked a background+kill shell pattern | Use `run_in_background` with until-loops, not `&` plus `kill` |
| `grep` is ugrep | `--exclude-dir` behaves differently; check warnings |
| The i5 often strands itself | A human may need to put it back on the dock after test runs |
