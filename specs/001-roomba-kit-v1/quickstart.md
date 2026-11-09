# Quickstart: Validating Roomba Kit V1

End-to-end scenarios that show the spec is met. They reference `contracts/` rather than restating
it.

## Prerequisites

- macOS or Linux host on the same LAN as the robots. Python 3.13+ and `uv` installed.
- A Roomba i5/i5+ with Clean Base and a Braava jet m6, both already on Wi-Fi via the iRobot app.
- The iRobot app closed on all phones during hardware runs (it competes for the single local
  connection).
- Optional: iRobot account credentials.

```bash
git clone git@github.com:Hfish21/roomba-kit.git && cd roomba-kit
uv sync
uv run pytest                      # unit + contract + integration (fake transport), no robots needed
```

## V1. First command from zero (US1, SC-001, SC-009)

```bash
uv run roomba-kit setup            # discover → password via HOME button → optional cloud → rooms
uv run roomba-kit service start    # expect: ready within 10 s
uv run roomba-kit robots list      # both robots, supported=true, connection=connected
uv run roomba-kit clean <vacuum>   # expect: outcome confirmed, activity=cleaning
uv run roomba-kit pause <vacuum> && uv run roomba-kit dock <vacuum>
```

Pass when a fresh user completes this in under 15 min using only `docs/getting-started.md`.

Variants:
- `setup` with a VPN active: discovery is empty. Rerun with `--broadcast 192.168.1.255`, or use
  `robots add <ip>` (FR-002).
- Decline cloud credentials: `cloud status` → `not_configured`, while local commands still work.

## V2. Live state and events (US2, SC-005, SC-006)

```bash
uv run roomba-kit events watch --robot <vacuum> &
uv run roomba-kit clean <vacuum>; uv run roomba-kit dock <vacuum>
```

Expect `mission.started` → `activity.changed(cleaning)` → `activity.changed(returning)` →
`robot.docked` → `mission.finished`, each within 2 s of the robot's change. Power-cycle the
router: expect `connection.changed` offline → connected within 60 s of Wi-Fi returning.

## V3. Room cleaning by name (US3)

```bash
uv run roomba-kit rooms import <vacuum>       # cloud configured
uv run roomba-kit rooms list <vacuum>
uv run roomba-kit clean <vacuum> --room "Laundry Room"
uv run roomba-kit missions wait <vacuum>      # terminal mission with rooms_requested=["7"]
```

Negative checks:
- `--room Nowhere` → `room_not_found` (exit 4).
- An empty room list → `invalid_request`.

## V4. Surface parity and auth (US4, SC-002, SC-008)

```bash
uv run pytest tests/contract                   # parity: every registry op on H/M/C, same schemas
curl -s localhost:8765/v1/robots               # → 401 unauthorized
curl -s -H "Authorization: Bearer $(cat ~/.local/state/roomba-kit/cli.token)" \
     localhost:8765/v1/robots/<vacuum>/state   # same JSON as: roomba-kit state <vacuum> --json
```

MCP check: point an MCP client at `uv run roomba-kit mcp` and ask: "clean the laundry room with
the vacuum and tell me when it's done". Pass when it uses `clean_rooms` and `mission_wait` and
reports the outcome. Then call `evac` on the mop and expect `not_supported`.

## V5. Settings with read-back (US5)

```bash
uv run roomba-kit verify <vacuum> setting.passes setting.edge_clean   # writes, reads back, restores
uv run roomba-kit settings set <mop> pad_wetness 2                    # applied | not_applied
uv run roomba-kit settings set <vacuum> pad_wetness 2                 # not_supported
```

## V6. Cloud extras and degradation (US6, SC-004)

- With cloud configured, run a room clean with `events watch --type cloud.timeline`. Expect
  travel, room and fin events during the run.
- After it finishes, run `coverage <vacuum> <mission>`. Expect `cloud_unavailable(not_yet_published)`
  at first, then points once the cloud publishes them.
- Block cloud egress (firewall, or `ROOMBA_KIT_CLOUD__PASSWORD=wrong`), then rerun V1–V3 and V5.
  Everything local passes, and cloud operations return `cloud_unavailable`.

## V7. Live position (US7)

- `position <mop> --watch` during a mop run gives fixes every 2 s or faster. While docked, it
  shows `no_fix`.
- `position <vacuum>` → `not_supported`.

## V8. Local history (US8)

- Run two missions, then run `service stop && service start`.
- `missions list` shows both, with outcome, rooms and initiator.
- Restarting mid-mission continues the same mission record.

## V9. Deploy to another host (US9)

```bash
docker compose -f deploy/compose.yaml up -d    # Linux host, network_mode: host
roomba-kit --service-url http://<host>:8765 robots list
```

## V10. Add a model (US10, SC-011)

- Add `tests/fixtures/profiles/fake_model/` and a test profile.
- `pytest tests/profiles` passes. `git diff --stat` touches no `api/`, `mcp/` or `cli/` files.
- An unknown SKU in discovery shows `supported=false`.

## Secrets (SC-007)

```bash
uv run pytest tests/integration/test_secret_leak.py   # canary creds; scans logs, responses, events
uv run roomba-kit config show                          # every secret shown as ***
```
