# Quickstart run: V1–V10 on the real robots

Run on 2026-11-09 (hardware session 1, "HW-1") against the owner's Roomba i5+ ("vacuum") and
Braava jet m6 ("mop") on the home LAN, with the service on the developer Mac, code from
`main` at each step. Results are what was observed; anything not run says so. Capability-level
evidence is in `docs/verification/roomba_i5.md` and `docs/verification/braava_m6.md`.

| # | Scenario | Result | Notes |
|---|---|---|---|
| V1 | First command from zero | ✅ pass, ⚠ two parts open | Discovery under the VPN found both robots via the configured subnet broadcast. Both paired by HOME-button retrieval through the kit (`robots password`), verified by connecting before storing. `service start` ready in 1.3 s (SC-009 ≤ 10 s). `robots list` → both `connected`. `clean` → `pause` → `dock` confirmed. **Open:** the interactive `setup` wizard itself wasn't run on hardware (its operations were), and the "fresh user in < 15 min from getting-started.md" walk-through needs the owner. |
| V2 | Live state and events | ✅ pass, ⚠ reconnect by other evidence | `mission.started` → `activity.changed(cleaning)` → … → `robot.docked` → `mission.finished` seen on real runs (event log). **Router power-cycle not run** (owner declined to drop the Wi-Fi). Reconnect evidence instead: the robots dropped their sessions on their own several times during HW-1 and the kit reconnected within 1–3 s each time; SC-006 (≤ 60 s) is proven by `tests/integration/test_timing.py` with the real backoff (cap 50 s). |
| V3 | Room cleaning by name | ✅ pass | `rooms import` (7 i5 rooms, 6 m6), `rooms list`, `clean <vacuum> --room "Laundry Room"` → `missions wait` → `completed`, `rooms_requested ["7"]`. `--room Nowhere` → `room_not_found`, exit 4, with 7 valid rooms. Empty room list → `invalid_request`. Stale-map protection also fired for real: the m6 saved a new map version mid-session and its room clean returned `map_changed` until rooms were re-imported. |
| V4 | Surface parity and auth | ✅ pass, ⚠ live agent run open | `pytest tests/contract` (parity, registry = contract, snapshots) passes. `GET /v1/robots` without a token → 401 (docs routes too). `settings show <mop> --json` byte-equal to the HTTP body. **Open:** asking a real MCP client "clean the laundry room…" over `roomba-kit mcp`; the same flow passes in `tests/integration/test_us4_mcp_agent_flow.py` (in-service and stdio). |
| V5 | Settings with read-back | ✅ pass | `verify` wrote, read back and restored all 6 i5 and 7 m6 settings. `settings set <mop> pad_wetness 2` → `applied`. `settings set <vacuum> pad_wetness 2` → `not_supported`, exit 5. |
| V6 | Cloud extras and degradation | ✅ pass (i5), ⚠ m6 timeline open | i5 room clean: cloud timeline `start`, `travel → room 7`, `room 7` within seconds. Coverage: i5 491 points, m6 2,258 points; unpublished missions → `cloud_unavailable(not_yet_published)`. Blocked cloud (wrong password): status `unavailable(auth_failed)`; `find` on both robots ~1 s; settings and local rooms fine; floor plan and history served from cache with `retrieved_at`; `rooms import` → `cloud_unavailable(auth_failed)`. **Open:** m6 timeline (blocked by the m6 tank fault). |
| V7 | Live position | ✅ pass | m6 fix while mopping at (−0.79, −1.60) m; docked m6 → `no_fix`. `position <vacuum>` → `not_supported`, exit 5. |
| V8 | Local history | ✅ pass | Missions recorded with outcome, rooms and initiator. Service restarted mid-clean: the same mission ID continued (`in_progress`, no duplicate) and closed when the robot docked. |
| V9 | Deploy to another host | ❌ not run | No Linux host identified yet. The image builds and runs (`docker build`, `compose config`, health check against the kit's own client); host-network discovery on Linux is untested. |
| V10 | Add a model | ✅ pass (tests) | `pytest tests/profiles` passes, incl. a fake model appearing on all three surfaces with no surface change. No third robot model was available for a live `supported=false` discovery. |
| Secrets | SC-007 | ✅ pass | `test_secret_leak.py` passes; `config show` on the real config masks 4 secret fields and contains no stored password. |

## Hardware findings that changed the kit

HW-1 found 17 real-robot behaviours or kit bugs; each was fixed with a regression test and
logged in `decisions.md` (#63–#77). Summary in `HANDOFF.md` ("Robot behaviour learned on
hardware").

## Still to do (owner)

- `train` mapping run on both robots (owner will clear the house; can change room IDs).
- m6 `clean_rooms` and `cloud.timeline` once the m6 detects its water tank again.
- V1 fresh-user walk-through, V4 live MCP agent run, V9 on a Linux host.
