# Implementation Plan: Roomba Kit V1

**Branch**: `001-roomba-kit-v1` | **Date**: 2026-11-08 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-roomba-kit-v1/spec.md`

## Summary

Build a Python package, `roomba-kit`, around one long-running async service that owns a local
session to each configured robot (Roomba i5/i5+, Braava jet m6) and an optional shared iRobot
cloud session. Every capability is defined once in an **operation registry**, and the
FastAPI HTTP API, the FastMCP server and the Click CLI are all generated from it. That gives the
three surfaces parity by construction.

The following core pieces sit behind kit-owned interfaces, so the transport libraries
(roombapy, roombapy-prime) stay inside two adapter modules:
- per-model **profiles** (capabilities and verification, setting mappings);
- a normalized state, activity and mission model;
- confirmed commands and read-back settings;
- an event bus with resumable sequence numbers (SSE, plus MCP long-poll);
- SQLite-backed local history.

Setup, config and secrets, single-command lifecycle, a hardware `verify` tool and docs
generated from the registry complete V1. Design decisions are in [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.13+ (CI matrix 3.13, 3.14)

**Primary Dependencies**: FastAPI 0.162, uvicorn 0.54, FastMCP 4.1, Click 8.5, pydantic 2.14 +
pydantic-settings, httpx, aiosqlite 0.22, sse-starlette 3.7, roombapy 2.0.2, roombapy-prime 0.8.3

**Storage**: SQLite (`<state>/state.db`) for history, events, rooms and cloud cache. TOML config
plus a 0600 credentials file.

**Testing**: pytest, pytest-asyncio; a FakeTransport replaying scrubbed captured payloads;
contract snapshots (OpenAPI, MCP tools, CLI help); hardware tests opt-in via the `hardware`
marker.

**Target Platform**: macOS and Linux hosts on the robots' LAN. Container for always-on Linux
hosts (`network_mode: host`).

**Project Type**: Single Python package. The CLI, the service (HTTP and MCP) and the library
all ship in one distribution.

**Performance Goals**:

| Goal | Target |
|---|---|
| Command confirmation | ≤10 s for p95 (SC-003) |
| Local event delivery | ≤2 s from robot message (SC-005) |
| Reconnect after outage | ≤60 s (SC-006) |
| Service ready after start | ≤10 s (SC-009) |

**Constraints**:
- One local connection per robot.
- The cloud is optional and unofficial.
- No secrets in any output.
- Host networking is required for discovery.

**Scale/Scope**: One household; V1 has 2 robot profiles, ≤10 robots, ≤10 concurrent consumers,
and an event rate of a few per second at peak.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | How the design satisfies it | Status |
|---|---|---|
| I. Local-first, cloud-optional | The cloud runs as an isolated subsystem with its own status and backoff (R12). Cloud ops return `cloud_unavailable`. SC-004 is tested by a cloud-blocked run (quickstart V6). | ✅ |
| II. Single connection owner | The service holds all robot sessions. The CLI and stdio MCP are HTTP clients (R1, R2). Setup operations run inside the service. | ✅ |
| III. One capability model, three surfaces | Surfaces are generated from the registry, and a contract test checks parity. **Exceptions** (credential intake and token admin) are justified under Complexity Tracking. | ✅ with justified exception |
| IV. Capability-driven models | Profiles carry capabilities with verification (R10). `not_supported` is returned before any robot contact. | ✅ |
| V. Isolated transport | Only `transport/local.py` and `transport/cloud.py` import the libraries, enforced by import-linter. | ✅ |
| VI. Secrets stay out | `credentials.toml` is 0600 and enforced; the log redaction filter is applied; the secret-leak test covers SC-007; secret-bearing ops are excluded from MCP. | ✅ |
| VII. Versioned contracts, BLID IDs | `/v1` routes, event schema `/v1`, MCP tool snapshot; robots are keyed by BLID throughout. | ✅ |
| VIII. Verified on hardware | Every capability carries Verification. The `verify` tool appends to the verification log (R16). CI runs only against the fake transport. | ✅ |
| IX. Docs part of done | The reference is generated from the registry, with a CI staleness check (R15). The quickstart scenarios map to the docs. | ✅ |
| Tech constraints | Python 3.13+, uv, Click, FastAPI, FastMCP, async; the API has auth; one-command start/stop; container deploy. | ✅ |

Post-design re-check (after Phase 1): unchanged, all ✅. The only exceptions are the ones
recorded under Complexity Tracking.

## Project Structure

### Documentation (this feature)

```text
specs/001-roomba-kit-v1/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── operations.md      # the registry: source of truth for all surfaces
│   ├── surfaces.md        # HTTP / MCP / CLI derivation rules
│   ├── errors.md
│   ├── events.md
│   ├── config.md
│   └── model-profile.md
├── checklists/requirements.md
└── tasks.md               # /speckit-tasks
```

### Source Code (repository root)

```text
pyproject.toml              # uv project; console script `roomba-kit`; entry-point group roomba_kit.profiles
src/roomba_kit/
├── __init__.py             # exports Client
├── client.py               # public async Python client for consumers (HTTP + SSE)
├── core/
│   ├── errors.py           # ErrorCode enum, KitError, surface mappings (contracts/errors.md)
│   ├── models.py           # Robot, RobotState, Activity, Mission, Room, PositionFix, … (data-model.md)
│   ├── capabilities.py     # Capability enum, Verification
│   ├── events.py           # Event envelope + EventType
│   └── registry.py         # Operation dataclass, @operation decorator, registry
├── profiles/
│   ├── base.py             # ModelProfile, SettingMapping, CommandSpec
│   ├── texts.py            # shared error/not-ready readable texts
│   ├── loader.py           # entry-point discovery, SKU matching
│   ├── roomba_i5.py
│   └── braava_m6.py
├── transport/              # ONLY place roombapy / roombapy-prime are imported
│   ├── base.py             # RobotTransport / CloudTransport protocols
│   ├── discovery.py        # UDP irobotmcs broadcast
│   ├── local.py            # roombapy adapter (session, commands, deltas, rrtp, password)
│   ├── cloud.py            # roombapy-prime adapter (login, pmaps, umf, history, timeline)
│   └── fake.py             # FakeTransport for tests + demo mode
├── service/
│   ├── app.py              # composition root, lifespan, startup/shutdown
│   ├── session.py          # RobotSession: connection supervisor, command queue, state normalizer
│   ├── normalize.py        # raw → RobotState / Activity (research R8)
│   ├── confirm.py          # confirmation predicates + windows (R9)
│   ├── settings.py         # read/write via profile mappings with read-back
│   ├── position.py         # rrtp poller (R13)
│   ├── alerts.py           # dock_search_timeout
│   ├── derive.py           # state → activity/mission/bin/error events
│   ├── cloud_data.py       # timeline watchers, floor plans, coverage, cache
│   ├── cloud.py            # CloudManager: login, backoff, rooms import, timeline, coverage
│   ├── rooms.py            # rooms/maps, learned rooms, name resolution
│   ├── eventbus.py         # seq, fan-out, persistence, filters
│   ├── history.py          # mission recorder
│   ├── store.py            # aiosqlite + migrations
│   ├── verify.py           # hardware verification runner
│   └── operations/         # registry handlers grouped by area (system, robots, commands, settings, rooms, events, history, cloud)
├── config/
│   ├── paths.py
│   ├── settings.py         # pydantic-settings, env overrides
│   ├── credentials.py      # 0600 enforcement, reload on mtime
│   ├── tokens.py
│   └── redact.py           # logging filter
├── api/
│   ├── app.py              # FastAPI factory, auth, error handler, /mcp mount
│   ├── generate.py         # registry → routes
│   └── sse.py
├── mcp/
│   ├── server.py           # registry → FastMCP tools (in-service)
│   └── stdio.py            # stdio proxy → service HTTP
├── cli/
│   ├── main.py             # Click root, global options
│   ├── generate.py         # registry → commands
│   ├── client.py           # httpx client of the service
│   ├── output.py           # human / --json rendering
│   ├── setup_wizard.py
│   ├── aliases.py          # `clean ROBOT [--room]` → clean | clean_rooms
│   ├── events_watch.py     # SSE renderer
│   ├── position_watch.py
│   └── lifecycle.py        # serve, service start/stop/status/install/logs, token, config path
└── lifecycle/
    ├── daemon.py           # detached process + pidfile
    ├── launchd.py
    └── systemd.py
tests/
├── unit/  ├── profiles/  ├── contract/  ├── integration/  ├── hardware/
└── fixtures/               # scrubbed captured payloads (state, timeline, umf)
docs/
├── index.md  getting-started.md  setup.md  deployment.md  troubleshooting.md
├── guides/ (building-consumers.md, adding-a-model.md)
├── reference/ (generated: operations.md, errors.md, events.md, support-matrix.md, cli.md)
└── verification/ (roomba_i5.md, braava_m6.md)
deploy/ (Dockerfile, compose.yaml); install templates ship in the package at
src/roomba_kit/lifecycle/templates/ (launchd.plist.tmpl, roomba-kit.service.tmpl; decisions.md #43)
scripts/gen_docs.py, scripts/capture_fixture.py (dev tool; not a kit surface)
.github/workflows/ci.yml
.importlinter, ruff.toml/pyproject sections, mkdocs.yml, README.md, CLAUDE.md, LICENSE
```

**Structure Decision**: a single package with a `src/` layout. The layering is enforced by
import-linter:

- `core` imports nothing internal. Handlers are typed against the `OperationContext`
  protocol, so `core` never imports `service`.
- `profiles` → `core`.
- `transport` → `core`.
- `service` → `core`, `profiles`, `transport`, `config`.
  `service/operations/__init__.py:load_operations()` imports the handler modules.
- `api`, `mcp` and `cli` → `core` and `service` (in-process), or `cli/client` (remote).
- `api`, `mcp` and `cli` never import `transport` **directly**. Indirect imports through
  `service` are expected, and the import-linter contracts set `allow_indirect_imports`.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| III: `token_create`, `token_list` and `token_revoke` are CLI-only, as local file operations | Creating a token must show the secret once. VI forbids secrets in HTTP/MCP responses. | Exposing them on HTTP/MCP would put a live credential in an API or LLM response |
| III: `cloud_login` and `set_robot_password` are on HTTP and CLI but not MCP | They take secrets as input. Routing account passwords through an LLM context is a leak path VI exists to prevent. | Keeping them on MCP for strict parity trades a real secret-exposure risk for symmetry. The parity test asserts this exclusion list explicitly. |
| III: `serve`, `service start\|stop\|status\|install\|uninstall\|logs`, `config path`, `version` and `mcp` are CLI-only and not registry operations | They manage the local kit process (start it, stop it, install it as an OS service, find its files, run the stdio MCP server). None is a robot or account capability, so FR-046's parity requirement, which covers capabilities, doesn't apply. Their HTTP-side counterparts (`health`, `service_status`, `config_show`) are operations. | Making them operations would expose "stop the service" or "install a launchd agent" over the very HTTP/MCP server they control, and a stopped service can't answer `service start` anyway |

## Open Risks Carried Into Tasks

1. Two concurrent per-robot cloud MQTT sessions under one login may evict each other (R12).
   Verify early; the fallback is a single session with multiple subscriptions.
2. Write paths for settings are unverified on both models. A task closes each one via
   `verify`.
3. Whether the m6 accepts room cleaning and emits a cloud timeline is unverified.
4. `SPECIFY_INIT_DIR=~/context` is set globally in `~/.claude/settings.json` (from the
   workbench template). The repo's `.specify/scripts/python/common.py` is patched so the nearest
   `.specify/` wins. Re-apply that patch if `specify init --force` regenerates the scripts.
