# Handoff: start here

**State (2026-11-10): V1 is complete and verified on both robots.** 108 of 110 tasks are done;
all operations in `contracts/operations.md` are on `main` across HTTP, MCP and CLI (a contract
test pins the registry to the spec), with ~735 mocked-transport tests, contract snapshots and
green CI on Python 3.13/3.14. Two independent spec-compliance audits were run and every gap they
found was fixed with a regression test.

**Hardware (HW-1 2026-11-09, HW-2 2026-11-10):** every declared capability on both robots is
`verified` with a cited run in `docs/verification/<profile>.md`, except `train` (owner's OK
pending). `checklists/quickstart-run.md` records V1–V10 as observed. Hardware testing found ~20
real-robot behaviours the kit now handles; each is in `decisions.md` (#63–#80) with a
regression test, and summarised in the table below.

**Remaining (no software changes needed):**

| Item | Owner | Notes |
|---|---|---|
| Install on the home server (Linux) | owner | This is V9 and settles FR-015 (systemd + linger, or the container in `deploy/`). See `docs/deployment.md`. |
| T104 `train` on both robots | owner | Only with the house clear; it can re-create the map and change room IDs (re-import rooms after). |
| T110 owner checks | owner | V1 fresh-user walk-through of `docs/getting-started.md`; V4 ask a real MCP agent to clean a room. |

Known operational notes: a sleeping laptop host stops the service; robots ignore `dock` while
cleaning (the kit pauses first) and for up to ~70 s after `stop` (the kit retries); the m6 is
slow to act mid-mop (90 s clean / 30 s pause-stop-dock windows).

Run it: `uv run roomba-kit setup` (or `robots add` + `robots password`), then
`uv run roomba-kit service start`. Reports from `roomba-kit verify … --write-report` land in
`docs/verification/<profile>.md`.

**Robot behaviour learned on hardware (HW-1)** — the kit handles these; know them when
debugging:

| Behaviour | Seen on | What the kit does |
|---|---|---|
| `dock` while cleaning is echoed but ignored | i5, m6 | Pauses first (`dock_needs_pause`) |
| `dock` soon after `stop` is ignored for 30–70 s | i5, m6 | Re-sends every 15 s while stopped off-dock |
| A `pause` seconds after leaving the dock ends the mission | i5 | `verify` cleans 30 s before pausing |
| Reboot is the firmware command `reset` | i5 | Profile protocol mapping |
| Links stay "connected" but go silent | i5, m6 | Reconnects on no reaction to a command, 2 min silence mid-mission, 10 min idle |
| Not-ready 68 for ~1.5 min after a bin evacuation; refuses to start | i5 | `verify` waits; neutral text |
| A robot-stored schedule opens a cycle that sits on the dock (cloud team routine) | i5 | Reported as docked with a mission pending |
| m6 acts on pause/stop/dock up to ~15 s late mid-mop | m6 | 30 s windows for the m6 |

## What this project is, and why

The owner has a Roomba i5+ and a Braava jet m6. The hardware is great, but the iRobot app and
cloud are not: schedules silently stopped firing, the app disconnects, and the robots' own
counters show hundreds of failed missions. iRobot went bankrupt in Dec 2025 and was sold to
Picea.

**Roomba Kit** is the owner's own foundation for controlling the robots:
- a local-first Python service that owns the robots' connections;
- exposed identically through a CLI, an HTTP API and an MCP server;
- built so automations, a Hermes Agent bot and a phone app can be layered on later, as separate
  projects.

It must be professional-grade: documented, tested, honest about what's verified, and easy for
humans and LLMs to build on.

## Read in this order

| # | Doc | Why |
|---|---|---|
| 1 | `.specify/memory/constitution.md` | 9 non-negotiable principles (local-first, single connection owner, one capability model / three surfaces, …) |
| 2 | `docs/research/discovery-2026-11-08.md` | What we proved on hardware, dead ends, environment gotchas |
| 3 | `specs/001-roomba-kit-v1/spec.md` | What to build: 10 user stories, 51 FRs, 11 SCs, scope limited to the two models |
| 4 | `specs/001-roomba-kit-v1/plan.md` + `research.md` | Architecture and the 16 design decisions |
| 5 | `specs/001-roomba-kit-v1/contracts/` | Operations registry, surfaces, errors, events, config, model-profile convention |
| 6 | `specs/001-roomba-kit-v1/data-model.md`, `quickstart.md` | Types, and the V1–V10 acceptance scenarios |
| 7 | `specs/001-roomba-kit-v1/tasks.md` | 110 tasks, 81 parallelizable, with dependencies and parallel lanes |
| 8 | `tests/fixtures/captures/README.md` | Scrubbed real payloads to build fakes from |

Owner context outside the repo (local only): `~/context/ideas/roomba-kit/ideation.md`,
`~/context/sessions/2026-11-08-roomba-local-control.md`.

## Decisions already made (don't re-litigate)

- Python 3.13+, uv, **Click / FastAPI / FastMCP** (owner's choice).
- V1 supports **exactly** the i5/i5+ and the jet m6, plus a documented convention for adding
  models. The 2025 lineup is out.
- Cloud features are used where they help and are never required. Automations, Hermes, the app
  and map editing are out of scope.
- Surfaces are generated from one operation registry. The only exceptions to surface parity are
  token admin (CLI-only) and `cloud_login`/`set_robot_password` (not MCP), per plan Complexity
  Tracking.
- Never run the `train` mapping run on hardware without the owner's explicit OK: it can change
  room IDs.

## Hardware testing

- **Robots:** both are on the home LAN. Their last-known IPs are in
  `~/context/sessions/2026-11-08-roomba-local-control.md`, but DHCP can change them, so discover
  with the subnet broadcast `192.168.1.255`.
- **Testing credentials:** `~/scratch/roomba-kit/roomba.env` holds `PATTY_*`, `EVA_*` and
  `IROBOT_*`. Never commit it, print it, or copy it into the repo.
- **Before any run:**
  - The robot must be docked and charged.
  - Nothing else may hold its local connection: no iRobot app, no stray scripts.
  - Ask the owner first if it will move, because the i5 may need retrieving.
