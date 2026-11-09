---
description: "Task list for Roomba Kit V1"
---

# Tasks: Roomba Kit V1

**Input**: Design documents from `specs/001-roomba-kit-v1/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Included. Constitution VIII requires a mocked-transport suite, and the spec's success
criteria are verified by named tests (parity, secret leak, cloud blocked, timing).

**Organization**: Grouped by user story so stories can be built and tested independently.

**Agent notes (read before any task)**:
- SpecKit scripts resolve this repo automatically. `.specify/scripts/python/common.py` is patched so the nearest `.specify/` wins over the global `SPECIFY_INIT_DIR=~/context` set in `~/.claude/settings.json`.
- Only `src/roomba_kit/transport/local.py` may import `roombapy`, and only
  `src/roomba_kit/transport/cloud.py` may import `roombapy_prime` (Constitution V,
  import-linter).
- Operations self-register. Every module in `src/roomba_kit/service/operations/` is
  auto-imported by `load_operations()` in `service/operations/__init__.py` (T012), so story
  phases add **new** files and never edit a shared operations index.
- Derived behaviour (events, history, alerts, position, learned rooms) subscribes to the
  EventBus. Story phases add new subscriber modules and don't edit `session.py`.
- Never write secrets to logs, fixtures, docs or test snapshots. Fixtures use the placeholder
  robot ID `00000000000000000000000000000001` and IPs in `192.0.2.0/24`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on incomplete tasks)
- **[Story]**: The user story (US1–US10) the task belongs to

---

## Phase 1: Setup (Shared Infrastructure)

- [x] T001 Initialize the uv project in `pyproject.toml`:
  - Package and Python:
    - name `roomba-kit`, `requires-python = ">=3.13"`, src layout, `.python-version` = `3.13`
  - Runtime deps:
    - `fastapi>=0.162`, `uvicorn>=0.54`, `fastmcp>=4.1,<5`, `click>=8.5`
    - `pydantic>=2.14`, `pydantic-settings`, `httpx`, `aiosqlite>=0.22`, `sse-starlette>=3.7`
    - `roombapy==2.0.2`, `roombapy-prime==0.8.3`
  - Dev group:
    - `pytest`, `pytest-asyncio`, `ruff`, `mypy`, `import-linter`, `mkdocs-material`
  - Console script `roomba-kit = "roomba_kit.cli.main:cli"`.
  - Entry-point group `roomba_kit.profiles`:
    - `roomba_i5 = "roomba_kit.profiles.roomba_i5:PROFILE"`
    - `braava_m6 = "roomba_kit.profiles.braava_m6:PROFILE"`
  - Tool config:
    - `[tool.ruff]` (line length 100; rules E, F, I, B, UP, ASYNC, S)
    - `[tool.mypy]` (strict for `roomba_kit.core`, `roomba_kit.service`, `roomba_kit.profiles`)
    - `[tool.pytest.ini_options]` (`asyncio_mode = "auto"`, markers `hardware`, `slow`)
  - Run `uv lock`.
- [x] T002 Create the package skeleton with an empty `__init__.py` in every directory listed under
  "Source Code" in `specs/001-roomba-kit-v1/plan.md` (`src/roomba_kit/{core,profiles,transport,service,service/operations,config,api,mcp,cli,lifecycle}`),
  plus `tests/{unit,profiles,contract,integration,hardware,fixtures}`. Put `__version__ = "0.1.0"`
  in `src/roomba_kit/__init__.py`.
- [x] T003 [P] Add layering contracts in `.importlinter`:
  - `core` imports no other roomba_kit package.
  - `profiles` → `core` only.
  - `transport` → `core` only.
  - `api`, `mcp` and `cli` must not import `roomba_kit.transport` **directly**. Set
    `allow_indirect_imports = True`: they legitimately reach `service`, which uses transport.
  - `roombapy` may be imported directly only by `roomba_kit.transport.local`, and
    `roombapy_prime` only by `roomba_kit.transport.cloud`. Again set
    `allow_indirect_imports = True`, because the rest of the package reaches them through
    `transport`.
- [x] T004 [P] Create `tests/conftest.py`:
  - A `--robot` CLI option.
  - Auto-skip `hardware`-marked tests unless `--robot` is given.
  - A `tmp_kit_home` fixture that sets `ROOMBA_KIT_CONFIG_DIR` and `ROOMBA_KIT_STATE_DIR` to
    temp dirs.
- [x] T005 [P] Create `.github/workflows/ci.yml`. On push/PR, with a matrix of Python 3.13 and
  3.14, run:
  - `uv sync --frozen`
  - `uv run ruff check`
  - `uv run ruff format --check`
  - `uv run mypy src`
  - `uv run lint-imports`
  - `uv run pytest -m "not hardware"`
  - `uv run python scripts/gen_docs.py --check`
- [x] T006 [P] Create `mkdocs.yml` (Material theme; nav: Home, Getting started, Setup, Guides,
  Reference, Deployment, Troubleshooting, Verification) and placeholder pages for every file listed
  under `docs/` in plan.md.
- [x] T007 [P] Create the repo-root files:
  - `.gitignore`: Python, `.venv`, `site/`, `*.env`, `*.token`, `state.db`
  - `LICENSE`: MIT, copyright Hayden Salmon 2026
  - `README.md`: one-paragraph purpose, install, link to the getting-started guide
  - `CLAUDE.md`: agent guide covering the layering rules, the operation registry pattern, the
    "new files per story" rule, the `SPECIFY_INIT_DIR` patch note, the no-secrets rule, the verify
    workflow, and the commands `uv run pytest`, `uv run roomba-kit serve`,
    `uv run python scripts/gen_docs.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ CRITICAL**: No user story work starts until this phase is complete.

### Core types

- [x] T008 [P] Implement `src/roomba_kit/core/errors.py`:
  - `ErrorCode` StrEnum with exactly the codes in `contracts/errors.md`.
  - `KitError(code, message, robot_id=None, details=None)`.
  - `HTTP_STATUS` and `CLI_EXIT` dicts copied verbatim from that table
    (e.g. `not_supported → 422 / 5`, `service_unavailable → exit 69`).
- [x] T009 [P] Implement `src/roomba_kit/core/capabilities.py`:
  - `Capability` StrEnum with exactly the values listed under "Capability (enum, V1)" in
    `data-model.md` (e.g. `clean_rooms`, `setting.pad_wetness`, `live_position`,
    `cloud.timeline`).
  - `Verification` (`verified(run: str)` / `unverified()`).
  - `CapabilityStatus` model.
- [x] T010 [P] Implement `src/roomba_kit/core/models.py`, the pydantic v2 models in
  `data-model.md` (no transport types):
  - `Robot`
  - `ConnectionState` (values `connected`, `connecting`, `offline`, `held_elsewhere`,
    `auth_failed`, `not_configured`)
  - `RobotState`
  - `Activity` (`docked`, `charging`, `cleaning`, `mapping`, `paused`, `returning`,
    `emptying_bin`, `stuck`, `unknown`)
  - `Initiator` (`kit`, `schedule`, `button`, `app`, `team`, `other`)
  - `MissionRef`, `CommandEcho`, `CodeText`, `MapRef`
  - `Room`, with the effective-name rule "`local_name`, then `cloud_name`, then `Room <id>`"
  - `LearnedRoom`
  - `CommandResult` (`outcome` ∈ `confirmed` | `unconfirmed`)
  - `SettingChange` (`outcome` ∈ `applied` | `not_applied`)
  - `Mission` (`outcome` ∈ `in_progress`, `completed`, `cancelled`, `stuck`, `failed`,
    `manually_docked`, `unknown`)
  - `ActivityTransition`, `PositionFix`, `CoverageRecord`
  - `DiscoveredRobot` (`id`, `name`, `sku`, `firmware`, `address`, `supported`,
    `profile_id`, `configured`)
- [x] T011 [P] Implement `src/roomba_kit/core/events.py`:
  - An `Event` envelope `{schema: "roomba-kit.event/v1", seq, type, robot_id, at, data}`.
  - An `EventType` StrEnum with exactly the types in `contracts/events.md`.
  - Typed `data` models per type.
- [x] T012 Implement `src/roomba_kit/core/registry.py`:
  - An `Operation` dataclass with fields `name`, `description`, `params` (pydantic model),
    `result` (model), `capability` (Capability | None), `read_only`, `motion`,
    `http: (method, path)`, `cli: (group path, name, arg hints)` and `surfaces` (default `{"http", "mcp", "cli"}`).
  - An `@operation(...)` decorator that registers async handlers
    `(ctx: OperationContext, params) -> result`. `OperationContext` is a `typing.Protocol`
    defined in `core/registry.py`, because `core` must not import `service`.
  - `get(name)` and `all()`.
  - Also write `src/roomba_kit/service/operations/__init__.py` with `load_operations()`. It
    imports every module in that package via `pkgutil.walk_packages`, so new modules
    self-register. Surfaces call it at startup.
  - Depends on T008–T011.
- [x] T013 Implement `src/roomba_kit/profiles/base.py` (`ModelProfile`, `SettingMapping(read, write,
  group, confirm)`, `CommandSpec(predicate, window_s)`) and `src/roomba_kit/profiles/loader.py`
  (load `roomba_kit.profiles` entry points, `match_profile(sku) -> ModelProfile | None` by regex
  `sku_patterns`) per `contracts/model-profile.md`.
- [x] T014 [P] Create `src/roomba_kit/profiles/roomba_i5.py`:
  - `PROFILE`: id `roomba_i5`, display name "Roomba i5/i5+", `sku_patterns=[r"^i5"]`.
  - Capabilities exactly as in the `roomba_i5` column of the data-model table:
    - **verified**: `clean`, `pause`, `resume`, `dock`, `find`, `clean_rooms`, `evac`,
      `state.bin`, `state.clean_base`, `cloud.rooms`, `cloud.history`, `cloud.coverage`,
      `cloud.timeline`. Each uses `run="session b10c0fbc 2026-11-08"`.
    - **unverified**: `stop`, `reboot`, `train`, and `setting.passes`, `setting.edge_clean`,
      `setting.always_finish`, `setting.child_lock`, `setting.eco_charge`,
      `setting.schedule_hold`.
    - **not declared**: `live_position`, `setting.carpet_boost`.
  - Leave `settings={}` (filled in T070).
- [x] T015 [P] Create `src/roomba_kit/profiles/braava_m6.py`:
  - `PROFILE`: id `braava_m6`, display name "Braava jet m6", `sku_patterns=[r"^m6"]`.
  - Capabilities per the `braava_m6` column:
    - **verified**: `clean`, `pause`, `resume`, `dock`, `find`, `state.pad`, `state.tank`,
      `state.lid`, `live_position`, `cloud.rooms`, `cloud.history`, `cloud.coverage`.
    - **unverified**: `stop`, `reboot`, `train`, `clean_rooms`, `cloud.timeline`, and
      `setting.passes`, `setting.edge_clean`, `setting.child_lock`, `setting.eco_charge`,
      `setting.schedule_hold`, `setting.pad_wetness`, `setting.spray_overlap`.
  - Leave `settings={}` (filled in T071).

### Transport

- [x] T016 Define `src/roomba_kit/transport/base.py` with the protocols below. Transport
  exceptions are raised as `KitError` with codes `robot_offline`, `connection_held_elsewhere`,
  `auth_failed` or `password_unavailable`.
  - **`RobotTransport`**:
    - `connect()`, `disconnect()`
    - `on_reported(cb)`, called with reported-state fragment dicts
    - `on_connection(cb)`
    - `send_command(name, params)`
    - `publish_settings(dict)`
    - `request_position(timeout)`, which returns `PositionFix`, returns `None` for no fix, or
      raises `TimeoutError` when not answered
  - **`PasswordRetriever.retrieve(address, timeout)`**
  - **`CloudTransport`**:
    - `login()`
    - `robot_password(blid)`
    - `pmaps(blid)`
    - `umf(blid, pmap_id, version)`
    - `mission_history(blid)`
    - `watch_timeline(blid)` (async iterator of dicts)
    - `close()`
- [x] T017 [P] Implement `src/roomba_kit/transport/discovery.py`:
  - Discovery: UDP broadcast of `b"irobotmcs"` to port 5678. The broadcast address defaults to
    `255.255.255.255`, or a caller-supplied address (e.g. `192.168.1.255`, needed when a VPN
    owns the default route). Re-send every 1 s until `timeout_s`.
  - Parse the JSON replies (`robotid`, `robotname`, `ip`, `mac`, `sw`, `sku`, `cap`, `ver`) and
    dedupe by `robotid`.
  - Unicast `probe(address)` for add-by-IP.
  - Return `DiscoveredRobot` with `supported`/`profile_id` from `match_profile(sku)`.
- [x] T018 [P] Implement `src/roomba_kit/transport/local.py`, the roombapy 2.0.2 adapter:
  - Connection and messages:
    - `RoombaClient(ip, blid, password)` as an async context manager.
    - `register_on_message_callback` → feed `state.reported` dicts to `on_reported`.
    - Map `RoombaAuthError` → `auth_failed`. Map refused or immediately closed connections
      while the robot answers discovery → `connection_held_elsewhere`. Otherwise → `robot_offline`.
  - Commands and settings:
    - `send_command(name, params)` → `client.send_command(name, params)`.
    - `publish_settings` → the library's delta/preference publish.
  - Position:
    - `request_position(timeout)` → `client.get_position(timeout=...)`.
  - Password:
    - `RoombaPassword(ip).get_password(timeout)`. A `None` result after the unsupported magic
      `f005efcc3b2903` maps to `password_unavailable`.
  - Never log payloads containing `password` or `passwordHash`.
- [x] T019 [P] Implement `src/roomba_kit/transport/cloud.py`, the roombapy-prime 0.8.3 adapter:
  - Login and REST:
    - `CloudAccount.login(session, username, password, country, app_id=f"IOS-{uuid4()}")`.
    - `account.classic_rest()` for `get_pmaps(blid)`, `get_pmap_umf(blid, pmap_id, version)`
      and `get_mission_history(blid)`.
    - Robot password from `account.login_result.raw["robots"][blid]["password"]`.
  - Timeline:
    - `PrimeFactory.create_prime_robot(session, username, password, country, blid=blid,
      login_result=account.login_result)`, then `await robot.connect()`.
    - Then `robot.watch_raw_topic(f"{prefix}/things/{blid}/mission/timeline/report")` with
      `prefix = login_result.irbt_topic_prefix_for(blid)`.
    - **Never publish to `.../req`**: AWS policy drops the session.
  - Helper: parse UMF `maps[].layers[]` by `layer_type` (`coverage`, `dirt`, `clutter`,
    `coverage_poly`) into coordinate lists.
- [x] T020 [P] Implement `src/roomba_kit/transport/fake.py`:
  - `FakeTransport`: scripted reported-state replay plus command reactions:
    - `start` → `phase:run, cycle:clean`; `pause` → `stop`; `resume` → `run`;
    - `dock` → `hmUsrDock` then `charge/none`;
    - `evac` → `cycle:evac`, then back;
    - configurable delay and drop.
  - `FakeCloud`: canned pmaps, umf, history and timeline.
  - `FakePasswordRetriever`.
  - Create scrubbed fixtures in `tests/fixtures/`:
    - `i5_state.json`, `m6_state.json`
    - `i5_pmaps.json`, `m6_pmaps.json`
    - `i5_mission_history.json`, `i5_mission_umf.json`
    - `i5_timeline.jsonl`
  - Build them from the scrubbed hardware captures already committed in
    `tests/fixtures/captures/` (see its README). Use those IDs and placeholder values as-is,
    and don't introduce real identifiers.

### Config & storage

- [x] T021 [P] Implement `src/roomba_kit/config/paths.py` and `src/roomba_kit/config/settings.py`
  per `contracts/config.md`:
  - XDG defaults plus `ROOMBA_KIT_CONFIG_DIR` / `ROOMBA_KIT_STATE_DIR`.
  - `config.toml` load and atomic save.
  - pydantic-settings env prefix `ROOMBA_KIT_` with `__` nesting. Precedence is env, then
    file, then defaults. Env-sourced values are never written back.
  - Defaults: `server.host=127.0.0.1`, `server.port=8765`, `alerts.dock_search_minutes=5`,
    `history.event_retention_days=30`.
- [x] T022 [P] Implement the three secrets modules:
  - `src/roomba_kit/config/credentials.py`:
    - `credentials.toml` is created with mode 0600 and written atomically.
    - Refuse to load if the mode is broader than 0600, raising a message that includes
      `chmod 600 <path>`.
    - Reload when mtime changes.
  - `src/roomba_kit/config/tokens.py`:
    - `create(name)` returns plaintext once and stores the SHA-256.
    - `verify(token)`, `list()`, `revoke(name)`.
    - The `cli` token plaintext lives at `<state>/cli.token`, mode 0600.
  - `src/roomba_kit/config/redact.py`: a `logging.Filter` that replaces every currently-known
    secret value and the patterns `password=…`, `Bearer …` and `"password": "…"` with `***`.
- [x] T023 [P] Implement `src/roomba_kit/service/store.py`:
  - An aiosqlite connection to `<state>/state.db` with a numbered-migration runner.
  - The v1 schema:
    - `missions`
    - `activity_transitions`
    - `events` (`seq INTEGER PRIMARY KEY AUTOINCREMENT`)
    - `rooms` (robot_id, map_id, map_version, room_id, cloud_name, local_name, type; unique
      robot_id + map_version + lower(effective name))
    - `learned_rooms`
    - `cloud_cache` (robot_id, kind, key, json, retrieved_at)
    - `verification_runs`
  - A daily event-retention prune.
- [x] T024 Implement `src/roomba_kit/service/eventbus.py`:
  - `publish(type, robot_id, data)` assigns `seq` and persists the event. `position` events
    are skipped for persistence unless `events.persist_position`.
  - Fan out to async subscribers with robot/type filters.
  - `read(since, robot, types, limit)` and `wait(since, robot, types, timeout_s)`.
  - An internal `subscribe(callback, types)` API for service modules. Depends on T011, T023.

### Service core

- [x] T025 [P] Implement `src/roomba_kit/service/normalize.py`, mapping a merged raw
  `state.reported` dict to `RobotState`:
  - **Activity mapping** (exactly research R8):
    - `phase=charge, cycle=none` → `docked`, or `charging` if `batPct<100`
    - `phase=run` → `cleaning`, or `mapping` when `cycle=train`
    - `phase=stop` with `cycle≠none` → `paused`
    - `hmUsrDock` / `hmPostMsn` / `hmMidMsn` → `returning`
    - `phase=evac` or `cycle=evac` → `emptying_bin`
    - `phase=stuck` → `stuck`
    - otherwise `unknown`
  - **Initiator**: `localApp` → `kit`, `schedule` → `schedule`, `team` → `team`,
    `manual` → `button`, else `app`/`other`.
  - **Counters**:
    - `bbmssn.nMssnOk` / `nMssnF` / `nMssnC` → missions ok / failed / cancelled
    - `bbrun.hr` → runtime hours
    - `bbrun.nStuck` → stuck count
  - **Other fields**:
    - `bin{present, full}`
    - `dock` → `clean_base`
    - `detectedPad` → pad
    - `tankPresent` / `tankLvl` → tank
    - `lidOpen`
    - `signal{rssi, snr}`
    - `pmaps[0]` → `MapRef`
    - `cleanSchedule2` → schedules
    - `lastCommand` → `CommandEcho`
  - Profile-gated fields are null when the profile lacks the capability.
- [x] T026 [P] Implement `src/roomba_kit/service/confirm.py`:
  - Default `CommandSpec`s exactly per research R9 (window in seconds):

    | Command | Confirmed when | Window |
    |---|---|---|
    | `clean` / `clean_rooms` | `cleaning` | 20 |
    | `pause` | `paused` | 10 |
    | `resume` | `cleaning` | 10 |
    | `stop` | cycle `none` | 10 |
    | `dock` | `returning` or `docked` | 10 |
    | `find` | `lastCommand.command=="find"` with a newer time | 5 |
    | `evac` | `emptying_bin` | 15 |
    | `reboot` | disconnect then reconnect | 180 |
    | `train` | `mapping` | 20 |

  - Profile `command_overrides` win.
  - `await_confirmation(session, spec)` returns `CommandResult` with `confirmed`, or
    `unconfirmed` plus the current state at window end.
- [ ] T027 Implement `src/roomba_kit/service/session.py` `RobotSession`:
  - **Connection supervision**:
    - Owns exactly one `RobotTransport`.
    - Supervisor with exponential backoff (1 s doubling to a 60 s cap).
    - `ConnectionState` transitions per `data-model.md`. `auth_failed` is terminal until
      credentials change.
    - After 3 consecutive offline attempts, rediscover the robot by BLID and update its
      address in config (FR-018).
  - **State**:
    - Merges reported fragments and normalizes on each update.
    - Publishes `connection.changed`, and `state.changed` coalesced to at most 1/s.
  - **Command execution**:
    - A per-robot FIFO `asyncio.Queue` for commands (FR-030).
    - `execute(command, params)`:
      1. If the profile lacks the capability, raise `not_supported` before any transport call
         (FR-023).
      2. Otherwise send the command and confirm it.
  - `apply_settings(...)` hook (used by US5).
  - Depends on T016, T024–T026.
- [ ] T028 Implement `src/roomba_kit/service/app.py` `ServiceContext`:
  - Loads settings, credentials, profiles, store and eventbus.
  - Creates a `RobotSession` per configured robot.
  - Exposes `cloud` (`CloudManager` interface, status `not_configured` until US1 T042).
  - `resolve_robot(ref)`, by BLID or unambiguous name, raising `robot_not_found` or
    `ambiguous_name` with candidates.
  - Startup/shutdown lifespan.
  - `ServiceContext` satisfies the `OperationContext` protocol from T012.
  - Depends on T021–T024, T027.

### Surfaces (generated)

- [ ] T029 Implement `src/roomba_kit/api/app.py` and `src/roomba_kit/api/generate.py`:
  - FastAPI factory, with the `ServiceContext` in the lifespan.
  - Bearer-token dependency (config/tokens) on every route except `GET /v1/health`.
  - `KitError` handler → `{"error":{"code","message","robot_id","details"}}` with
    `HTTP_STATUS`. `CommandResult(outcome=unconfirmed)` → 202.
  - Generate routes from the registry following `contracts/surfaces.md` rules.
  - OpenAPI at `/v1/openapi.json`, docs at `/v1/docs`.
  - A `stream_presenters` hook dict, so US2 can attach SSE to `events_list`.
  - Mount the MCP app from T030 at `/mcp` behind the same auth.
- [ ] T030 [P] Implement `src/roomba_kit/mcp/server.py`:
  - Build a `FastMCP("roomba-kit")` with one tool per registry operation whose `surfaces`
    include `mcp`.
  - Each generated description contains:
    - the purpose;
    - parameter docs;
    - "Supported on: <profile ids with the capability>";
    - "Verification: <profile: status>";
    - "⚠ Moves the robot" for `motion` ops.
  - Set `readOnlyHint` on read-only ops.
  - `KitError` → tool error text starting with the code.
  - Expose `http_app(transport="http")`.
- [ ] T031 [P] Implement the CLI core:
  - `src/roomba_kit/cli/main.py`: Click root with `--json`, `--service-url` (default
    `http://127.0.0.1:8765`) and `--quiet`.
  - `src/roomba_kit/cli/client.py`: an httpx client. The token comes from `ROOMBA_KIT_API_TOKEN`
    or `<state>/cli.token`. On connection refused, print
    `Service not running — start it with: roomba-kit service start` and exit 69.
  - `src/roomba_kit/cli/output.py`: human tables. `--json` prints the exact HTTP response body.
  - `src/roomba_kit/cli/generate.py`: registry → Click commands per the `contracts/surfaces.md`
    command tree. Exit codes come from `CLI_EXIT`; `unconfirmed` exits 3.
- [ ] T032 Implement `src/roomba_kit/cli/lifecycle.py` and `src/roomba_kit/lifecycle/daemon.py`:
  - `serve`: foreground uvicorn on the configured host and port.
  - `service start`: spawn a detached `serve` and write `<state>/service.pid`. Poll
    `/v1/health` until ready (fail after 10 s, SC-009). Logs go to `<state>/service.log`.
  - `service stop`: SIGTERM, wait 10 s, then SIGKILL; remove the pidfile.
  - `service status`: pid, health, `service_status`.
  - `service logs [-f]`.
  - `config path`, `token create|list|revoke` (local file ops), `version`.

### Foundational tests & docs tooling

- [x] T033 [P] Unit tests in `tests/unit/test_normalize.py`. Feed fixtures plus synthetic
  phase/cycle combos covering every R8 row, including `hmPostMsn` → `returning` and `evac`.
- [x] T034 [P] Unit tests in `tests/unit/test_confirm.py`:
  - A pause that lands 5 s late → `confirmed`.
  - A pause that never lands → `unconfirmed` with state.
  - `find` echo detection.
- [x] T035 [P] Unit tests in `tests/unit/test_config.py`:
  - Path resolution, and env precedence over file.
  - A mode-0644 credentials file is refused with the chmod hint.
  - The redact filter masks canary values.
  - Tokens are stored hashed.
- [ ] T036 [P] Unit tests in `tests/unit/test_session.py` with FakeTransport:
  - Backoff timing.
  - `held_elsewhere` detection.
  - `not_supported` raised without a transport call.
  - FIFO ordering under concurrent `execute` calls.
- [ ] T037 [P] Contract test `tests/contract/test_parity.py`:
  - Every registry operation appears on HTTP (OpenAPI), MCP (tool list) and CLI (Click
    tree), with the same parameter names and result schema.
  - The only exclusions allowed are exactly `set_robot_password` and `cloud_login` (not MCP)
    and `token_create`, `token_list` and `token_revoke` (CLI only), per the plan's Complexity
    Tracking.
- [x] T038 [P] Contract test `tests/contract/test_errors.py`: `ErrorCode`, `HTTP_STATUS` and
  `CLI_EXIT` equal the table parsed from `specs/001-roomba-kit-v1/contracts/errors.md`.
- [ ] T039 [P] Implement `scripts/gen_docs.py`:
  - Generates these files from the registry, profiles, error catalogue, event types and Click
    tree:
    - `docs/reference/operations.md`, with HTTP, MCP and CLI usage for each op
    - `docs/reference/errors.md`
    - `docs/reference/events.md`
    - `docs/reference/support-matrix.md` (profiles × capabilities with ✅/⚪/—)
    - `docs/reference/cli.md`
  - `--check` exits non-zero when the files are stale.

**Checkpoint**: `uv run pytest`, `ruff`, `mypy` and `lint-imports` pass, and
`uv run roomba-kit serve` starts with zero robots.

---

## Phase 3: User Story 1 - Set up and command a robot without the iRobot app (P1) 🎯 MVP

**Goal**: guided setup, a robot registered, the service started with one command, and confirmed
clean/pause/resume/stop/dock/find.

**Independent Test**: quickstart V1 (hardware), or `tests/integration/test_us1_setup_and_commands.py`
(fake).

- [ ] T040 [P] [US1] Integration test `tests/integration/test_us1_setup_and_commands.py`, using
  FakeTransport:
  - `discover_robots` lists both fixtures with `supported=true`, and an unknown SKU gives
    `supported=false`.
  - `retrieve_password` → `stored`, and the password never appears in any response.
  - `password_unavailable` path → `cloud_only`.
  - `add_robot` by address.
  - `clean`/`pause`/`resume`/`stop`/`dock`/`find` → `confirmed`.
  - With cloud declined, `cloud_status` → `not_configured`.
- [ ] T041 [P] [US1] Implement `src/roomba_kit/service/operations/setup.py`:
  - `discover_robots(broadcast?, timeout_s=8)`, marking `configured` and `supported`.
  - `add_robot(address)`.
  - `retrieve_password(robot, timeout_s=60)`:
    - Retry every 2 s.
    - `steps` is the text `["Place the robot on its dock", "Press and hold HOME for about 2
      seconds until it chimes", "Release — retrieving…"]`.
    - On success, validate by connecting a session before saving to `credentials.toml`.
    - Outcome is `stored` | `timeout` | `cloud_only`.
  - `set_robot_password(robot, password)` with `surfaces={"http","cli"}`, validated before saving.
  - `remove_robot(robot)`, which stops the session and deletes the config and credential entries.
- [ ] T042 [P] [US1] Implement `src/roomba_kit/service/cloud.py` `CloudManager`:
  - A single shared login.
  - Status `not_configured` | `connected` | `unavailable(reason)`.
  - Exponential backoff capped at 15 min. Rejected credentials stop retrying until they change.
  - Publishes `cloud.status`.
  - `robot_password(blid)`.
  - Also implement `src/roomba_kit/service/operations/cloud_account.py`:
    - `cloud_login(username, password, country="US")` with `surfaces={"http","cli"}`, validated
      before saving.
    - `cloud_logout`.
    - `cloud_status`.
    - `cloud_retrieve_password(robot)` → `stored` | `unavailable`.
- [ ] T043 [P] [US1] Implement `src/roomba_kit/service/operations/inventory.py`: `list_robots`,
  `get_robot` and `get_capabilities`, returning `Robot` with capabilities and verification.
  Robots without a profile are listed with `supported=false` and can't be commanded (FR-024).
- [ ] T044 [P] [US1] Implement `src/roomba_kit/service/operations/commands.py`:
  - `clean`, `pause`, `resume`, `stop`, `dock`, `find`, `evac`, `reboot` and `train`.
  - Each calls `session.execute` and returns `CommandResult`.
  - Each declares `motion=True`, except `find`.
- [ ] T045 [P] [US1] Implement `src/roomba_kit/service/operations/system.py`: `health` (all surfaces;
  the HTTP route needs no auth), `service_status`, and `config_show` (all secrets rendered as `"***"`).
- [ ] T046 [US1] Implement `src/roomba_kit/cli/setup_wizard.py` (`roomba-kit setup`). It calls
  only registry operations through the client, in this order:
  1. Offer `service start` if the service isn't running.
  2. `discover_robots`. If it finds nothing, explain VPN/subnet and prompt for a broadcast
     address or an IP (`add_robot`).
  3. For each robot, print the numbered password steps with a countdown and offer a retry.
  4. Explain what cloud credentials add (rooms, floor plans, history, coverage, live
     timeline, password fallback) and that they're optional. Prompt with `getpass`.
  5. Offer `cloud_retrieve_password` for `cloud_only` robots.
  6. Call `import_rooms` if that operation exists.
  7. On first run, create the `cli` token.
  8. Print next commands.

  When robots are already configured, start instead with a menu (FR-007):
  - add a robot;
  - re-pair a robot (retrieve or set its password again);
  - add, change or remove cloud credentials;
  - re-import rooms;
  - remove a robot.

  Each option changes only its own config and credential entries.
- [ ] T047 [P] [US1] Write `docs/getting-started.md` (install → `setup` → `service start` →
  `clean` in under 15 min) and `docs/setup.md`, covering:
  - password steps per model;
  - cloud credentials: what they add, optional, where they're stored;
  - re-running setup;
  - `--broadcast` and add by IP;
  - removing robots.

**Checkpoint**: MVP. A real robot is controllable end to end without the iRobot app.

---

## Phase 4: User Story 2 - See what every robot is doing, live (P1)

**Goal**: full state on demand plus a resumable push event stream.

**Independent Test**: quickstart V2, or `tests/integration/test_us2_state_events.py`.

- [ ] T048 [P] [US2] Integration test `tests/integration/test_us2_state_events.py`:
  - A replayed mission produces `mission.started` → `activity.changed(cleaning)` →
    `activity.changed(returning)` → `robot.docked` → `mission.finished`.
  - A drop and restore produces `connection.changed` events.
  - Error codes carry text.
  - `events_wait` resumes from `since`.
  - `alert.dock_search_timeout` fires once after the threshold.
- [ ] T049 [P] [US2] Implement `src/roomba_kit/service/operations/state.py`: `get_state(robot)`
  returning the full `RobotState` (FR-026, FR-027).
- [ ] T050 [P] [US2] Implement `src/roomba_kit/service/derive.py`. It subscribes to session state
  updates and publishes:
  - `activity.changed`;
  - `mission.started` (cycle none→X) and `mission.finished` (X→none);
  - `robot.docked`, `robot.stuck`;
  - `bin.full` (false→true);
  - `error.raised` / `error.cleared` (kind error or not_ready);
  - `setting.changed`.

  Mission outcome rules:
  - `done` / `plcDoc` → `manually_docked`;
  - stop → `cancelled`;
  - stuck → `stuck`;
  - error → `failed`;
  - otherwise `completed`.
- [ ] T051 [P] [US2] Implement `src/roomba_kit/service/alerts.py`: when activity has been
  `returning` longer than `alerts.dock_search_minutes` (default 5), publish
  `alert.dock_search_timeout {mission_id, minutes}`, once per mission.
- [ ] T052 [P] [US2] Implement `src/roomba_kit/service/operations/events.py`:
  - `events_list(since?, robot?, types?, limit=100)`.
  - `events_wait(since, robot?, types?, timeout_s=30, max 300)`.
  - `mission_wait(robot, mission_id?, timeout_s=1800)`. Returns the terminal `Mission`, or
    `{timeout:true, mission}`.
- [ ] T053 [P] [US2] Implement `src/roomba_kit/api/sse.py` and register it in
  `stream_presenters` for `events_list`: with `Accept: text/event-stream`, stream frames with
  `id=<seq>` and `event=<type>`, honouring `Last-Event-ID` (sse-starlette).
- [ ] T054 [P] [US2] Implement `src/roomba_kit/cli/events_watch.py` (`roomba-kit events watch
  [--robot] [--type …]`): consume SSE and render one line per event, or JSON lines with
  `--json`.
- [ ] T055 [P] [US2] Create `src/roomba_kit/profiles/texts.py`, a shared readable-text table for
  error and not-ready codes. Source it from roombapy `const.py` and `vendor_errors.py`. Include
  i3/i5-class `notReady` 68 = "Robot is off the dock", and 23 and 39 as transient lifecycle
  states. `normalize.py` uses profile overrides first, then this table.
- [ ] T056 [P] [US2] Write `docs/state-and-events.md`: state fields, activity semantics, event
  types with examples, resuming with `since` / `Last-Event-ID`, and alerts.

---

## Phase 5: User Story 3 - Clean specific rooms by name (P2)

**Goal**: room import and local naming, plus ordered room cleaning with stale-map protection.

**Independent Test**: quickstart V3, or `tests/integration/test_us3_rooms.py`.

- [ ] T057 [P] [US3] Integration test `tests/integration/test_us3_rooms.py`:
  - Import from FakeCloud, including unnamed m6 rooms that get placeholders.
  - `clean_rooms` by name sends `regions` in order.
  - Empty list → `invalid_request`.
  - Unknown name → `room_not_found` with `valid`.
  - Changed map version → `map_changed`.
  - Learned rooms are captured from `lastCommand.regions` without the cloud.
- [ ] T058 [US3] Implement `src/roomba_kit/service/rooms.py`:
  - Room queries on the store.
  - The effective-name rule.
  - Case-insensitive unique names per robot and map version.
  - Resolution of names/IDs → `room_not_found` / `ambiguous_name`.
  - The staleness check against the current `MapRef.version` → `map_changed`.
  - An EventBus subscriber that records `lastCommand.regions` + `pmap_id` into `learned_rooms`
    (FR-032).
  - Cloud import via `CloudManager` → `transport.cloud.pmaps(blid)`. Use
    `active_pmapv_details.regions[]` `{id, name, region_type}` and `active_pmapv_id`, and cache
    in `cloud_cache` (FR-043).
- [ ] T059 [US3] Implement `src/roomba_kit/service/operations/rooms.py`:
  - `list_rooms`, `import_rooms` (cap `cloud.rooms`) and `name_room`.
  - `clean_rooms(robot, rooms, ordered=true, room_options?)` (cap `clean_rooms`, `motion=True`):
    - Build params `{pmap_id, user_pmapv_id, ordered: 1|0, regions:[{region_id, type:"rid",
      params?}]}`.
    - Reject an empty room list with `invalid_request` (an empty list means whole home to the
      robot).
    - Confirm via `confirm.py`.
- [ ] T060 [P] [US3] Implement `src/roomba_kit/cli/aliases.py`: `roomba-kit clean ROBOT [--room
  NAME_OR_ID …] [--unordered]` dispatches to `clean` with no `--room`, otherwise to
  `clean_rooms`.
- [ ] T061 [P] [US3] Write `docs/rooms.md`: importing, local naming without the cloud, learned
  rooms, ordering, and the map-changed recovery.

---

## Phase 6: User Story 4 - Build on the kit as a developer or an LLM agent (P2)

**Goal**: surfaces that are provably identical, authenticated, documented and usable by agents.

**Independent Test**: quickstart V4 and the contract test suite.

- [ ] T062 [P] [US4] Snapshot tests, with snapshots in `tests/contract/snapshots/`:
  - `tests/contract/test_openapi_snapshot.py`
  - `tests/contract/test_mcp_tools_snapshot.py`, covering names, descriptions and schemas
  - `tests/contract/test_cli_help_snapshot.py`
- [ ] T063 [P] [US4] Integration test `tests/integration/test_us4_auth.py`:
  - Every non-health route and `/mcp` returns 401 / `unauthorized` without a token.
  - A token revoked via the CLI stops working without a service restart (mtime reload).
- [ ] T064 [P] [US4] Integration test `tests/integration/test_us4_json_parity.py`: for every read
  operation, `roomba-kit … --json` stdout equals the HTTP response body byte for byte (Click
  `CliRunner` against an in-process app).
- [ ] T065 [P] [US4] Implement `src/roomba_kit/mcp/stdio.py` (`roomba-kit mcp`): a stdio FastMCP
  server whose tools are generated from the same registry and proxy to the service HTTP API with
  the CLI token. When the service is down, every call returns `service_unavailable`.
- [ ] T066 [P] [US4] Integration test `tests/integration/test_us4_mcp_agent_flow.py`, using the
  fastmcp in-memory client:
  - `list_robots` → `clean_rooms(rooms=["Laundry Room"])` → `mission_wait` returns a terminal
    mission.
  - `evac` on the m6 → a tool error starting with `not_supported`.
- [ ] T067 [P] [US4] Implement `src/roomba_kit/client.py`, a public async Python client for
  consumers. It wraps HTTP with one typed method per registry operation, plus an `events()`
  async iterator over SSE with automatic `since` resume. Export `Client` from
  `src/roomba_kit/__init__.py`.
- [ ] T068 [P] [US4] Write `docs/guides/building-consumers.md`, covering:
  - tokens (`token create`);
  - HTTP and SSE resume;
  - Python `Client`;
  - MCP config snippets for stdio (`roomba-kit mcp`) and remote HTTP (`/mcp`), including a Hermes
    Agent example;
  - error handling and `unconfirmed`;
  - a worked scheduler example.

---

## Phase 7: User Story 5 - Read and change robot settings (P2)

**Goal**: settings read/write confirmed by read-back, plus the `verify` tool that closes
unverified capabilities.

**Independent Test**: quickstart V5, or `tests/integration/test_us5_settings.py`.

- [ ] T069 [P] [US5] Integration test `tests/integration/test_us5_settings.py`:
  - `passes=two` writes both keys together.
  - A read-back mismatch → `not_applied` with `actual`.
  - `pad_wetness` on i5 → `not_supported`.
  - `schedule_hold` round trip.
- [ ] T070 [P] [US5] Fill `settings` in `src/roomba_kit/profiles/roomba_i5.py`:
  - `passes`: write `noAutoPasses` and `twoPass` together.

    | Value | `noAutoPasses` | `twoPass` |
    |---|---|---|
    | `auto` | false | false |
    | `one` | true | false |
    | `two` | true | true |

  - `edge_clean` = `not openOnly`.
  - `always_finish` = `not binPause`.
  - `child_lock` = `childLock`.
  - `eco_charge` = `ecoCharge`.
  - `schedule_hold` = `schedHold`.
  - No `carpet_boost`: the i5 drops `carpetBoost`/`vacHigh` writes.
- [ ] T071 [P] [US5] Fill `settings` in `src/roomba_kit/profiles/braava_m6.py`:
  - `passes`, `edge_clean`, `child_lock`, `eco_charge`, `schedule_hold`: same mappings as T070.
  - `pad_wetness` (1–3) → `padWetness {"disposable": v, "reusable": v}`.
  - `spray_overlap` → `rankOverlap`: `standard`=67, `deep`=85, `extended`=25.
- [ ] T072 [US5] Implement `src/roomba_kit/service/settings.py`:
  - Read via `SettingMapping.read`.
  - Write the full `group` in one publish.
  - Wait up to 10 s for `confirm`, then return `SettingChange` `applied`, or `not_applied`
    with the actual value.
  - Route through the session's command queue for ordering.
- [ ] T073 [US5] Implement `src/roomba_kit/service/operations/settings.py`: `get_settings(robot)`
  and `set_setting(robot, setting, value)`, validating values per `contracts/operations.md`.
- [ ] T074 [US5] Implement the `verify` operation in `src/roomba_kit/service/verify.py` and
  `src/roomba_kit/service/operations/verify.py`:
  - Signature: `verify(robot, capabilities?, allow_motion=false)`.
  - Settings checks: write an alternate value, read it back, restore the original, and report
    pass/fail with evidence.
  - Command checks: issue and confirm. Skip unless `allow_motion`.
  - `train` is never run unless named explicitly, because it can re-create the map and change
    room IDs.
  - Results are stored in `verification_runs` and returned with a markdown report.
  - The CLI `verify --write-report` appends the report to `docs/verification/<profile>.md`.
- [ ] T075 [P] [US5] Write `docs/settings.md`, covering each setting per model, `not_applied`
  semantics, schedule hold vs. app schedules, and how `verify` works.

---

## Phase 8: User Story 6 - Use cloud extras when available (P3)

**Goal**: floor plans, cloud history, coverage and live timeline, all degrading cleanly.

**Independent Test**: quickstart V6, or `tests/integration/test_us6_cloud.py`.

- [x] T076 [US6] Hardware spike (research R12 open risk): with real credentials, open timeline
  sessions for both robots at once via `transport/cloud.py`, and check whether either evicts the
  other. Look for disconnect/reconnect loops in roombapy-prime logs. Record the result in
  `specs/001-roomba-kit-v1/research.md` under R12. Pick per-robot sessions, or one session
  subscribed to both robots' timeline topics.
- [ ] T077 [P] [US6] Integration test `tests/integration/test_us6_cloud.py`, using FakeCloud:
  - Timeline dicts become `cloud.timeline` events.
  - `get_coverage` returns `cloud_unavailable(not_yet_published)` and then points.
  - Cloud failure → local ops unaffected, and cloud ops return `cloud_unavailable(reason)`.
  - The cached floor plan is served with `retrieved_at` while the cloud is down.
- [ ] T078 [US6] Implement `src/roomba_kit/service/cloud_data.py`:
  - Timeline watchers, using the T076 strategy, per robot with `cloud.timeline` capability.
    Parse `event[]` / `finEvents[]` types (`start`, `travel{dest, rid}`, `room`,
    `traversal{rid}`, `evac`, `fin`) into `cloud.timeline {mission_id, kind, room_id?, dest?,
    raw_type}`.
  - Floor plan from pmaps plus UMF.
  - Coverage: find the mission in `mission_history` (`pmaps_info[0].pmap_id/pmapv_id`), then
    fetch UMF and extract the `coverage`, `dirt` and `clutter` layers.
  - Cache results in `cloud_cache`. Every failure → `cloud_unavailable(reason)`.
- [ ] T079 [US6] Implement `src/roomba_kit/service/operations/cloud_data.py`:
  - `get_floor_plan(robot)`
  - `cloud_history(robot, limit=30)`
  - `get_coverage(robot, mission_id)`
- [ ] T080 [P] [US6] Write `docs/cloud.md`, covering:
  - what cloud credentials add;
  - the unofficial-API caveat;
  - degradation behaviour;
  - coverage publishing lag (minutes after docking);
  - how to remove cloud credentials.

---

## Phase 9: User Story 7 - Follow a robot's live position (P3)

**Goal**: position fixes for capable models, with clean `not_supported` / `no_fix` semantics.

**Independent Test**: quickstart V7, or `tests/integration/test_us7_position.py`.

- [ ] T081 [P] [US7] Integration test `tests/integration/test_us7_position.py`:
  - m6 fixture during cleaning → `position` events at intervals of 2 s or less.
  - Docked → `no_fix`.
  - i5 → `not_supported` with no transport call.
  - A robot that never answers → polling stops after 3 timeouts.
- [ ] T082 [US7] Implement `src/roomba_kit/service/position.py` (research R13):
  - Poll only for profiles with `live_position`, only while activity is `cleaning` or
    `returning`.
  - Run at 0.5 Hz with a 2 s timeout.
  - Stop for the mission after 3 consecutive timeouts on a robot that has never answered.
  - Publish `position` events.
- [ ] T083 [P] [US7] Implement `src/roomba_kit/service/operations/position.py` (`get_position`)
  and `src/roomba_kit/cli/position_watch.py` (`roomba-kit position ROBOT --watch`, which
  renders the stream).
- [ ] T084 [P] [US7] Write `docs/position.md`: which models support it, coordinate frame (metres
  from the dock, heading in radians that wraps at π), `no_fix`, and the cloud timeline as the
  room-level alternative.

---

## Phase 10: User Story 8 - Keep a local record of every mission (P3)

**Goal**: durable local mission history that survives restarts.

**Independent Test**: quickstart V8, or `tests/integration/test_us8_history.py`.

- [ ] T085 [P] [US8] Integration test `tests/integration/test_us8_history.py`:
  - Two missions are persisted with outcome, rooms and initiator.
  - Filters work.
  - A service restart mid-mission continues the same mission ID, with no duplicates.
- [ ] T086 [US8] Implement `src/roomba_kit/service/history.py`:
  - Subscribe to `mission.started`, `mission.finished` and `activity.changed`.
  - Persist `Mission` and `ActivityTransition` rows.
  - On startup, re-attach an `in_progress` mission by robot mission ID.
  - Append every `error.raised` code seen during a mission to `Mission.errors` (FR-044).
  - When cloud history later shows the mission, set `cloud_mission_ref`.
- [ ] T087 [US8] Implement `src/roomba_kit/service/operations/history.py`:
  `list_missions(robot?, since?, until?, outcome?, limit=50)` and `get_mission(mission_id)`.
- [ ] T088 [P] [US8] Write `docs/history.md`.

---

## Phase 11: User Story 9 - Run the service on an always-on host (P3)

**Goal**: start-on-boot installs and a container deployment.

**Independent Test**: quickstart V9.

- [ ] T089 [P] [US9] Implement `src/roomba_kit/lifecycle/launchd.py` and
  `deploy/launchd.plist.j2`:
  - Install to `~/Library/LaunchAgents/com.roomba-kit.service.plist` with `RunAtLoad` and
    `KeepAlive`, logging to `<state>/service.log`.
  - `install` / `uninstall` use `launchctl bootstrap|bootout gui/$UID`.
- [ ] T090 [P] [US9] Implement `src/roomba_kit/lifecycle/systemd.py` and
  `deploy/roomba-kit.service.j2`:
  - A `systemd --user` unit with `Restart=on-failure`.
  - `install` runs `daemon-reload` and `enable --now`, and tells the user about
    `loginctl enable-linger` for boot without login.
- [ ] T091 [P] [US9] Create the container files:
  - `deploy/Dockerfile`:
    - `python:3.13-slim` base with `uv sync --frozen --no-dev`.
    - Runs as a non-root user.
    - `ROOMBA_KIT_CONFIG_DIR=/config` and `ROOMBA_KIT_STATE_DIR=/state` as volumes.
    - `CMD ["roomba-kit","serve"]`.
    - `server.host=0.0.0.0` set via env.
  - `deploy/compose.yaml`: `network_mode: host` (required for discovery), volumes, an env file
    reference, `restart: unless-stopped`.
- [ ] T092 [US9] Add `service install` and `service uninstall` to
  `src/roomba_kit/cli/lifecycle.py`, dispatching to launchd on macOS and systemd on Linux.
- [ ] T093 [P] [US9] Unit test `tests/unit/test_lifecycle_templates.py`: rendered plist and unit
  contents (paths, `KeepAlive`, `Restart`).
- [ ] T094 [P] [US9] Write `docs/deployment.md`, covering:
  - a Linux always-on host;
  - the container (host networking; Docker Desktop on macOS can't discover, so add robots by
    IP);
  - env-only configuration;
  - LAN exposure with `server.host` plus tokens;
  - backups (copy the config and state dirs).

---

## Phase 12: User Story 10 - Add support for another robot model (P3)

**Goal**: a documented, tested convention for adding a model with no surface changes.

**Independent Test**: quickstart V10.

- [ ] T095 [P] [US10] Profile tests:
  - `tests/profiles/test_profiles.py`: replay `tests/fixtures/profiles/<id>/state.json` for each
    shipped profile and assert the normalized state and capability set.
  - `tests/profiles/test_fake_model.py`:
    - Register a test profile via a patched entry-point list and assert it appears on HTTP, MCP
      and CLI.
    - Assert that no module under `api/`, `mcp/` or `cli/` references a profile ID, so adding a
      profile needs no surface change.
    - Assert that an unknown SKU → `unsupported_model` on commands.
- [ ] T096 [P] [US10] Implement `scripts/capture_fixture.py`, a developer tool and not a kit
  surface:
  - Refuse if the service pidfile is live.
  - Connect directly via `transport/local.py` using stored credentials.
  - Collect 30 s of reported state.
  - Scrub BLID, IP, MAC, `passwordHash`, `netinfo`, `wlcfg` and SSID.
  - Write `tests/fixtures/profiles/<id>/state.json`.
- [ ] T097 [P] [US10] Write `docs/guides/adding-a-model.md`, following the five-step workflow in
  `contracts/model-profile.md`, with a worked example based on `braava_m6.py`.

---

## Phase 13: Hardware Verification (closes Constitution VIII for V1)

**Purpose**: move every capability to `verified` or document why it stays `unverified`. Run on
real robots, one robot at a time (each robot allows one connection). i5 and m6 tasks can run in
parallel. Each task:
1. Runs `roomba-kit verify <robot> <caps…> [--allow-motion] --write-report`.
2. Commits the report.
3. Updates `Verification` in the profile file with `run="docs/verification/<profile>.md#<anchor>"`.

- [ ] T098 [P] Re-verify the i5's initially-verified set (SC-010): `clean`, `pause`, `resume`,
  `dock`, `find`, `evac`, `clean_rooms`, state fields and the `cloud.*` capabilities. Results go
  to `docs/verification/roomba_i5.md`.
- [ ] T099 Verify i5 `stop` and `reboot` with `--allow-motion` → `docs/verification/roomba_i5.md`;
  update `src/roomba_kit/profiles/roomba_i5.py`.
- [ ] T100 Verify i5 settings → `docs/verification/roomba_i5.md`, then update
  `src/roomba_kit/profiles/roomba_i5.py`. The settings are `setting.passes`,
  `setting.edge_clean`, `setting.always_finish`, `setting.child_lock`, `setting.eco_charge`
  and `setting.schedule_hold`. For `schedule_hold`, also confirm that a robot-stored schedule
  does not fire while held.
- [ ] T101 [P] Re-verify the m6's initially-verified set: `clean`, `pause`, `resume`, `dock`,
  `find`, `live_position`, state fields and the `cloud.*` capabilities. Results go to
  `docs/verification/braava_m6.md`.
- [ ] T102 Verify m6 `stop`, `reboot`, `clean_rooms` (name a room locally first) and
  `cloud.timeline` → `docs/verification/braava_m6.md`; update
  `src/roomba_kit/profiles/braava_m6.py`.
- [ ] T103 Verify m6 settings → `docs/verification/braava_m6.md`, then update
  `src/roomba_kit/profiles/braava_m6.py`. The settings are `setting.passes`,
  `setting.edge_clean`, `setting.child_lock`, `setting.eco_charge`, `setting.schedule_hold`,
  `setting.pad_wetness` and `setting.spray_overlap`.
- [ ] T104 `train` (both robots) is verified **only with the owner's explicit go-ahead**, since it
  may re-create the map and change room IDs. Otherwise it stays `unverified` with that reason
  noted in both verification docs. Afterwards, regenerate `docs/reference/support-matrix.md`
  with `scripts/gen_docs.py`.

---

## Phase 14: Polish & Cross-Cutting Concerns

- [ ] T105 [P] Write `docs/troubleshooting.md`, covering:
  - `connection_held_elsewhere`: close the iRobot app, or remove other integrations;
  - empty discovery under a VPN (`--broadcast`, add by IP);
  - `dock_search_timeout`: dock placement and clearance, clean the sensors;
  - cloud `auth_failed` / `cloud_unavailable`;
  - `unconfirmed` outcomes;
  - file permission errors.
- [ ] T106 [P] Finalize `docs/index.md`, the `mkdocs.yml` nav and `README.md` (feature list,
  support matrix link, quickstart).
- [ ] T107 [P] Secret-leak test `tests/integration/test_secret_leak.py` (SC-007):
  - Configure canary robot and cloud passwords and a token.
  - Run every operation on every surface against fakes, including the error paths.
  - Capture logs, CLI output, HTTP bodies, MCP results and persisted events.
  - Assert that no canary appears.
- [ ] T108 [P] Cloud-blocked test `tests/integration/test_cloud_blocked.py` (SC-004): with
  FakeCloud raising on every call, run the US1, US2, US3 (local-name path), US5 and US8
  integration flows. All pass, and cloud ops return `cloud_unavailable`.
- [ ] T109 [P] Timing test `tests/integration/test_timing.py`, using fake latency:
  - Command confirmation p95 is 10 s or less (SC-003).
  - Event latency from fake robot message to SSE frame is 2 s or less (SC-005).
  - Reconnect is within 60 s of the robot returning (SC-006).
  - `service start` readiness is 10 s or less (SC-009).
- [ ] T110 Run `uv run python scripts/gen_docs.py` and confirm CI is green. Then run quickstart
  V1–V10 on the real robots and record pass/fail with notes in
  `specs/001-roomba-kit-v1/checklists/quickstart-run.md`.

---

## Dependencies & Execution Order

### Phase dependencies

- Setup (T001–T007): T001 → T002, then T003–T007 run in parallel.
- Foundational (T008–T039) depends on Setup and blocks all stories. Internal order:
  1. T008–T011 in parallel.
  2. T012.
  3. T013, then T014–T015 in parallel.
  4. T016, then T017–T020 in parallel.
  5. T021–T023 in parallel, then T024.
  6. T025–T026 in parallel.
  7. T027.
  8. T028.
  9. T029, then T030–T031 in parallel.
  10. T032.
  11. T033–T039 in parallel, each after the code it tests.
- **User stories** (Phases 3–12) all depend only on Foundational:
  - US1 has no story dependencies (MVP).
  - US2 is independent. Its events are consumed by US4 (`mission_wait`) and US8.
  - US3 uses `CloudManager` from US1 T042 for `import_rooms`. Its local-name path works without it.
  - US4's T066 needs US2 T052 (`mission_wait`) and US3 T059 (`clean_rooms`). Its other US4
    tasks are independent.
  - US5 is independent.
  - US6 needs US1 T042 (`CloudManager`).
  - US7 is independent.
  - US8 needs US2 T050 (mission events).
  - US9 needs only T032.
  - US10 is independent.
- **Hardware Verification** (Phase 13) needs US5 T074 (`verify`) plus the ops being verified.
- **Polish** (Phase 14) comes after the desired stories.

### Within each story

The test task is written first and fails. Then come the service module(s), then operations,
then CLI or presentation extras, then docs.

## Parallel Opportunities

- **Phase 2**: up to 6 agents at once:
  - core types (T008–T011);
  - transport adapters (T017–T020);
  - config and storage (T021–T023);
  - normalize/confirm (T025–T026);
  - MCP and CLI generators (T030, T031);
  - the foundational tests (T033–T039).
- **After Phase 2**: one agent per story phase. US1, US2, US5, US7, US9 and US10 have no
  cross-story file overlap. Each story adds new files under `service/operations/` and new
  subscriber modules.
- **Same-file constraints**:
  - `profiles/roomba_i5.py` and `profiles/braava_m6.py` are edited in Foundational (T014,
    T015), US5 (T070, T071) and Phase 13. Serialize those.
  - `cli/lifecycle.py` is edited in T032 and T092.

## Parallel Example: after Foundational

```text
Agent A: Phase 3 (US1)  T040 → T041,T042,T043,T044,T045 [P] → T046 → T047
Agent B: Phase 4 (US2)  T048 → T049,T050,T051,T052,T053,T054,T055 [P] → T056
Agent C: Phase 7 (US5)  T069 → T070,T071 [P] → T072 → T073 → T074 → T075
Agent D: Phase 9 (US7)  T081 → T082 → T083,T084 [P]
Agent E: Phase 11 (US9) T089,T090,T091,T093,T094 [P] → T092
Agent F: Phase 12 (US10) T095,T096,T097 [P]
```

## Implementation Strategy

### MVP first

1. Phase 1, then Phase 2.
2. Phase 3 (US1): validate with quickstart V1 on the i5. This is the "replace the app for
   everyday control" milestone.
3. Phase 4 (US2): live state and events. This unblocks every consumer.

### Incremental delivery

Then US3 (room cleaning), US5 (settings + `verify`) and US4 (consumer hardening), then US6–US10.
Run Phase 13 as soon as `verify` lands, so the capabilities marked verified match what's true
on the hardware. Phase 14 closes V1.

## Notes

- Commit after each task or logical group; every commit passes CI.
- Hardware tasks need the owner present: robots move and may need to be retrieved (the i5 has
  intermittent docking failures).
- Never paste real BLIDs, IPs or passwords into code, fixtures, docs or commit messages.
