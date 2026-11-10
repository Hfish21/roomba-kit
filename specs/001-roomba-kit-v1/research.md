# Research: Roomba Kit V1

Phase 0 decisions. Hardware and protocol facts come from session `b10c0fbc` (2026-11-08,
`recall --show b10c0fbc`) unless another source is cited. Every Technical Context unknown is
resolved below.

## R1. Overall shape: one service, three surfaces generated from one operation registry

- **Decision**: A single long-running service process owns every robot session. The core
  defines an **operation registry**: each operation has a name, params model, result model,
  required capability, and handler. All three surfaces are generated from that registry:
  - FastAPI routes;
  - FastMCP tools;
  - Click commands, with per-operation CLI presentation hints.

  The CLI is an HTTP client of the service.
- **Rationale**: Constitution II forces a single connection owner. Generating surfaces from one
  registry makes Constitution III (identical surfaces) structural rather than a discipline, and
  makes SC-002 checkable by a parity test.
- **Alternatives**:
  - Hand-written routes, tools and commands kept in sync by review. These drift.
  - `FastMCP.from_fastapi`, generating MCP from OpenAPI. Tool names and descriptions are
    second-class, and it only covers MCP.
  - CLI connecting to robots directly. Violates II.

## R2. MCP transport and hosting

- **Decision**: The service mounts `FastMCP.http_app(transport="http")` (streamable HTTP) at
  `/mcp`, behind the same token auth as the API. `roomba-kit mcp` runs a **stdio** MCP server
  for local agent clients. It proxies each tool call to the running service's HTTP API using
  the same registry, so it never touches robots.
- **Rationale**: Remote agents such as Hermes on another host need network MCP. Desktop agents
  expect stdio. FastMCP 4.1.6 provides `http_app()`, `run_stdio_async()` and a per-tool `auth`
  hook (verified by inspecting the installed package).
- **Alternatives**:
  - SSE transport. Legacy in MCP.
  - A separate MCP process with its own robot connections. Violates II.

## R3. Event delivery

- **Decision**: An in-process **EventBus** assigns each event a monotonically increasing
  sequence number and persists a bounded event log.
  - HTTP: Server-Sent Events at `GET /v1/events`, with `since`, `robot` and `types` filters,
    plus a JSON page form for catch-up.
  - MCP: tools `events_wait(since, robot?, types?, timeout_s)` and
    `mission_wait(robot, mission_id?, timeout_s)`.
  - CLI: `events watch`.
- **Rationale**: SSE is one-way, HTTP-native and proxy-friendly, which fits push-only events.
  Sequence numbers let consumers resume after a disconnect. MCP clients handle long-poll
  tools more reliably than server notifications. SC-005 is measured from the robot message
  to SSE emit.
- **Alternatives**:
  - WebSockets. Bidirectionality isn't needed.
  - Webhooks. Push-to-consumer infrastructure; defer to consumers.
  - MCP resource subscriptions. Client support is uneven.

## R4. Transport adapters and library facts

- **Decision**: Use two adapters behind a `RobotTransport` / `CloudTransport` protocol, plus a
  `FakeTransport` for tests.
- **Local adapter** wraps roombapy 2.0.2 (MIT).
  - Connection: `RoombaClient(ip, blid, password)`, an async context manager with a supervised
    reconnect.
  - Messages: `register_on_message_callback`.
  - Commands: `send_command(cmd, params)`. Settings: `set_preference`, which publishes a
    `delta`.
  - Position: `get_position(timeout)`, which returns a pose, returns `None` for "no fix", or
    raises `TimeoutError` when the robot doesn't support it.
  - `RoombaPassword(ip).get_password(timeout)` retrieves the local password.
  - Discovery is done by the kit itself: a UDP broadcast of `irobotmcs` to port 5678, parsing
    the JSON replies.
- **Cloud adapter** wraps roombapy-prime 0.8.3 (MIT).
  - `CloudAccount.login(session, user, pw, country, app_id)` does the login.
  - `classic_rest()` provides `get_pmaps`, `get_pmap_umf`, `get_mission_history`.
  - `PrimeFactory.create_prime_robot(..., blid, login_result)` opens AWS IoT MQTT for a Classic
    robot, since `CloudAccount.prime_robot` refuses Classic robots.
  - `watch_raw_topic` subscribes to `{irbt_prefix}/things/{blid}/mission/timeline/report`.
- **Verified facts** (session b10c0fbc):
  - Room clean params: `{pmap_id, user_pmapv_id, ordered:1, regions:[{region_id, type:"rid"}]}`.
  - Passing `dock` while docked, or `evac`, empties the bin.
  - The i5+ never answers rrtp: 368 silent requests while cleaning.
  - The m6 answers rrtp about every 2 s while cleaning, with a position fix.
  - Publishing to `.../req` on the cloud is rejected by AWS policy and drops the session.
    `.../mission/rrtp/request` is acknowledged by the broker but the robot never answers.
  - `livemap/update` is silent for Classic robots.
  - The timeline reports `travel` (room/dock), `room`, `traversal{rid}`, `evac` and `fin`
    events in near-real time.
  - The cloud mission record for a run is not published until after the mission ends, with a
    lag of minutes.
- **Rationale**: Constitution V. The adapters are the only modules allowed to import these
  libraries, which an import-linter contract enforces.
- **Alternatives**: Re-implementing MQTT/TLS directly. More code and more risk, with no benefit
  while both libraries are maintained.

## R5. Configuration, secrets, and paths

- **Decision**: Use XDG-style paths on both macOS and Linux:
  - config dir `$ROOMBA_KIT_CONFIG_DIR`, else `$XDG_CONFIG_HOME/roomba-kit`, else
    `~/.config/roomba-kit`;
  - state dir `$ROOMBA_KIT_STATE_DIR`, else `$XDG_STATE_HOME/roomba-kit`, else
    `~/.local/state/roomba-kit`.

  Files:
  - `config.toml`: non-secret. Robots by BLID, display name, last IP, model profile ID,
    server bind, thresholds.
  - `credentials.toml`: mode 0600. Robot passwords, cloud username and password, API tokens
    (hashed).

  pydantic-settings maps env vars `ROOMBA_KIT_<SECTION>__<KEY>` over both files. The service
  refuses to start if `credentials.toml` is group- or world-readable (FR-013).
- **Rationale**: one documented place, easy to copy to another host or inject in a
  container (FR-009, FR-010). XDG paths keep docs identical across platforms.
- **Alternatives**:
  - OS keychain. Doesn't survive headless and container deploys uniformly; can come later
    as a backend.
  - platformdirs native macOS paths. Two sets of docs.
  - `.env` in the repo. Violates VI.

## R6. API authentication

- **Decision**: Bearer tokens. Setup creates a `cli` token for the local CLI and stdio MCP
  proxy. `token create --name <consumer>` mints more. Tokens are stored hashed (SHA-256) and
  are revocable.
  - The health endpoint is `/v1/health` and needs no auth.
  - Default bind is `127.0.0.1:8765`. LAN exposure is opt-in via `server.host`.
  - MCP over HTTP reuses the same middleware.
- **Rationale**: FR-048 and ideation S5. Simple for a single household, and works for the CLI,
  a phone app and Hermes alike.
- **Alternatives**:
  - OAuth. Overkill for one household.
  - mTLS. Hostile to phone and bot clients.

## R7. Persistence

- **Decision**: One SQLite database (`state.db`) in the state dir, accessed through aiosqlite,
  with versioned migrations applied at startup. Tables: missions, activity transitions, events
  (bounded, retention default 30 days), rooms (imported and local names, map version), learned
  room IDs, cloud cache (rooms, floor plans, coverage), tokens metadata, verification log.
  History itself is kept indefinitely by default.
- **Rationale**: Zero-ops and single-file, so it's trivially portable to another host, and
  enough for one household's write rate (a few events per second at peak).
- **Alternatives**: JSON files (no querying for FR-045), Postgres (ops burden).

## R8. Normalized activity model

- **Decision**: Map raw `cleanMissionStatus.{phase, cycle}` to a kit `Activity` enum:
  - `phase=charge` → `docked` (also `charging` while `batPct < 100`), with any `cycle`: an open
    cycle on the dock is a mission waiting there (HW-1 finding, decisions.md #63);
  - `phase=run` → `cleaning`, or `mapping` when `cycle=train`;
  - `phase=stop, cycle≠none` → `paused`;
  - `phase` in `hmUsrDock`, `hmPostMsn`, `hmMidMsn` → `returning`;
  - `phase=evac` or `cycle=evac` → `emptying_bin`;
  - `phase=stuck` → `stuck`;
  - anything else → `unknown`, with the raw value kept in diagnostics only.

  A mission begins at `cycle` none→X and ends at X→none. Its outcome comes from the end
  condition: completed, cancelled (stop), stuck, failed (error), or `manually_docked`
  (`plcDoc`).
- **Rationale**: consumers need stable semantics (Constitution V and VII). The phase and cycle
  values listed above were all observed during session b10c0fbc runs.

## R9. Command confirmation

- **Decision**: Each command defines an expected-state predicate and a confirmation window:

  | Command | Confirmed when | Window |
  |---|---|---|
  | `start` / `clean_rooms` | activity becomes `cleaning` | 20 s |
  | `pause` | `paused` | 10 s |
  | `resume` | `cleaning` | 10 s |
  | `stop` | cycle `none` | 10 s |
  | `dock` | `returning` or `docked` | 10 s |
  | `find` | `lastCommand` echo | 5 s |
  | `evac` | `emptying_bin` | 15 s |
  | `reboot` | disconnect, then reconnect | 180 s |
  | `train` | `mapping` | 20 s |

  Results are `confirmed` or `unconfirmed`. Commands are serialized per robot through an
  asyncio queue.
- **Rationale**: FR-029 and FR-030. On the m6, a pause registered about 5 s late, which the
  window absorbs.

## R10. Model profiles: the add-a-model convention

- **Decision**: One Python module per model in `roomba_kit/profiles/`, each exporting a
  `ModelProfile`. A profile contains:
  - an id, display name and SKU patterns;
  - a capability → `Verification` map;
  - setting mappings (logical setting → protocol keys, encode/decode, write groups);
  - per-command overrides;
  - an error-code text table.

  Profiles are auto-discovered by an entry-point group, `roomba_kit.profiles`, so
  out-of-tree profiles also work. V1 ships `roomba_i5` and `braava_m6`. Robots matching no
  SKU pattern are listed as `unsupported`.
- **Rationale**: FR-021 to FR-025 and SC-011. A model is data plus small mapping functions,
  with no surface changes.
- **Alternatives**: TOML profiles. These can't express encode/decode or write-group logic
  without an expression language.

## R11. Service lifecycle and deployment

- **Decision**:
  - `roomba-kit serve` runs in the foreground (uvicorn).
  - `roomba-kit service start|stop|status|logs` manages a detached background process with a
    pidfile in the state dir.
  - `roomba-kit service install|uninstall` writes a launchd agent (macOS) or a systemd user
    unit (Linux) for start-on-boot.
  - A `Dockerfile` and `compose.yaml` cover always-on hosts. The container uses
    `network_mode: host`, because UDP broadcast discovery needs the host network. On Docker
    Desktop for macOS, discovery is unavailable and robots must be added by IP; the docs say
    so.
- **Rationale**: FR-014, FR-015, R9 and R10.
- **Alternatives**: Running only under a system supervisor. That fails "one command to start"
  on a laptop.

## R12. Cloud session strategy

- **Decision**: One `CloudAccount` login per configured account, shared by all robots. It is
  refreshed via `relogin` on 403 or near token expiry. The live timeline uses one shared cloud MQTT
  session for all robots (see Result). Rooms and floor plans are imported at
  setup, on map version change, and on demand, and cached (FR-043). Cloud failures put the
  cloud subsystem into `unavailable(reason)` with exponential backoff, capped at 15 min.
- **Open risk (resolved by T076, below)**: whether two concurrent per-robot cloud MQTT
  sessions under one login coexist. roombapy-prime logs show AWS evicting duplicate
  `client_id`s. If they collide, fall back to one session subscribed to both robots' timeline
  topics.
- **Result (T076, 2026-11-08, hardware spike, read-only, both robots docked):** one
  `CloudAccount` login, robots' cloud MQTT sessions via `create_prime_robot(blid=…,
  login_result=…)`, each watching `{prefix}/things/{blid}/#`:

  | Run (90 s each) | Sessions | Drops |
  |---|---|---|
  | One robot | 1 | 0 |
  | Both robots, one session each | 2 | 105 (`[code:128]`, both sessions, roughly every 1.5 s, from the first second) |
  | One session subscribed to both robots' topics | 1 | 0 |

  The two per-robot sessions evict each other. **Strategy: one shared cloud MQTT session per
  account, subscribed to every robot's timeline topic.** Per-robot sessions are rejected.
  *Unverified:* that a second robot's timeline messages are actually delivered over the shared
  session. The robots were idle, so no messages arrived in any run. This is checked during the
  first hardware session with a mission (T102, m6 `cloud.timeline`).
- **Rationale**: FR-041, FR-042 and Constitution I.

## R13. Position polling

- **Decision**: Only for profiles declaring `live_position`, and only while activity is
  `cleaning`, `mapping` (a `train` run) or `returning`. Poll at 0.5 Hz using a 2 s timeout. Stop for the rest of the
  mission after 3 consecutive timeouts on a robot that has never answered. Treat `None` as
  "no fix".
- **Rationale**: FR-039 and FR-040. The m6 answered on that cadence in testing, and the i5 is
  declared without the capability, so it's never polled.

## R14. Testing strategy

- **Decision**: pytest + pytest-asyncio.
  - **unit**: normalizers, profiles, confirmation logic, config.
  - **contract**:
    - OpenAPI snapshot, MCP tool list snapshot, CLI help snapshot;
    - registry parity test (SC-002);
    - error-code catalogue test.
  - **integration**: the full service using `FakeTransport`, replaying scrubbed state payloads
    captured in session b10c0fbc.
  - **hardware**: marker `hardware`, opt-in via `--robot`, never run in CI. Writes results to
    the verification log.
  - **secret-leak test** (SC-007): runs the integration suite with canary credentials and
    scans all logs, responses and events for them.
- Tooling: ruff (lint and format), mypy (strict on `core`, `service` and `profiles`),
  import-linter (Constitution V). CI runs on GitHub Actions.

## R15. Documentation

- **Decision**: Markdown in `docs/`, built with MkDocs Material (dev dependency).
  - `docs/reference/` (operations, errors, events, support matrix) is **generated from the
    registry and profiles** by `scripts/gen_docs.py`. CI fails if the generated docs are
    stale.
  - Hand-written: getting started, setup, deployment, troubleshooting, the consumer guide, and
    the add-a-model guide.
- **Rationale**: Constitution IX and SC-002. The reference can't drift from the code.

## R16. Hardware verification tooling

- **Decision**: `roomba-kit verify <robot> [capability…]` runs a scripted, reversible check per
  capability:
  - settings are written, read back and restored;
  - commands are issued and confirmed;
  - for movement it prompts the user before acting.

  Each run appends to `docs/verification/<profile>.md` via a generated report the user
  commits.
- **Rationale**: Constitution VIII and SC-010. The unverified set (settings writes, schedule
  hold, m6 room clean, m6 timeline, train, reboot) is closed through this tool.

## Dependency summary

| Package | Version (2026-11-08) | License | Why |
|---|---|---|---|
| roombapy | 2.0.2 | MIT | Local transport |
| roombapy-prime | 0.8.3 | MIT | Cloud transport |
| fastapi | 0.162.x | MIT | HTTP API (constitution) |
| uvicorn | 0.54.x | BSD-3 | ASGI server |
| fastmcp | 4.1.x | Apache-2.0 | MCP server (constitution) |
| click | 8.5.x | BSD-3 | CLI (constitution) |
| pydantic / pydantic-settings | 2.14.x | MIT | Models, config + env overrides |
| httpx | current | BSD-3 | CLI and stdio-MCP client of the service |
| aiosqlite | 0.22.x | MIT | Async SQLite |
| sse-starlette | 3.7.x | BSD-3 | SSE responses |
| dev: pytest, pytest-asyncio, ruff, mypy, import-linter, mkdocs-material | current | various | Quality gates, docs |
