# Handoff: start here

**State (2026-11-08):** discovery is done, and the spec, plan and tasks are complete, analysed and
pushed. **No product code exists yet.** The next step is implementing
`specs/001-roomba-kit-v1/tasks.md` (110 tasks).

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
