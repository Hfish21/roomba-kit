---
slug: "roomba-kit"
date: "2026-11-08"
status: ready
provenance: chained
schema: 1
speckit_version: "1.2.1.dev0"
inputs:
  - ~/context/ideas/roomba-kit/ideation.md
  - .specify/memory/constitution.md
---

# Feature Specification: Roomba Kit V1

**Created**: 2026-11-08

**Status**: Ready

**Input**: User description: "Roomba Kit V1: a control kit for iRobot Wi-Fi robots (vacuums and mops) that a long-running local service exposes through a CLI, an authenticated HTTP API and an MCP server, so humans, programs and LLM agents can discover, configure, monitor and command robots without the iRobot app." Full description and feature areas A–N: `~/context/ideas/roomba-kit/ideation.md` plus the `/speckit-specify` invocation of 2026-11-08.

## Problem

Owners of iRobot Wi-Fi robots depend on the vendor app and cloud to schedule, start and monitor them,
and that dependency is unreliable. The user's two robots logged 186 and 134 failed missions, and
their cloud-coordinated schedules silently stopped firing months ago while still showing as
enabled. The vendor's future is uncertain after its 2025 bankruptcy and sale. The robots themselves
can be controlled directly on the home network, but there is no dependable, documented, general way
to do that. Anyone wanting automations, a chat-bot integration or their own app must reverse-engineer
the protocol and juggle the robot's one-client connection limit first.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Set up and command a robot without the iRobot app (Priority: P1)

A home owner installs the kit, runs one guided setup, and ends with every robot on their network
registered and controllable. Setup finds the robots, walks them through retrieving each robot's
local password (place the robot on its dock, hold HOME until it chimes), optionally takes their
iRobot account credentials after explaining what those add, and checks every credential by
actually connecting. They start the service with one command and tell a robot to clean, pause and
go home from the command line.

**Why this priority**: Nothing else is usable until a robot is registered and controllable. This
slice alone already replaces the app for everyday control.

**Independent Test**: On a clean machine on the same network as a supported robot, follow only the
getting-started guide, then run start, pause and dock commands and observe the robot respond.

**Acceptance Scenarios**:

1. **Given** a supported robot on the home Wi-Fi and no prior configuration, **When** the user runs
   setup, **Then** the robot is discovered and listed with its name, model, firmware and network
   address.
2. **Given** a discovered robot on its dock, **When** the user follows the on-screen password steps,
   **Then** the robot's password is retrieved, verified by a successful connection, and stored
   without being displayed.
3. **Given** the user declines to enter iRobot account credentials, **When** setup completes,
   **Then** all local control works and the cloud-backed features are listed as unavailable with
   the reason.
4. **Given** a completed setup, **When** the user runs the single start-service command and then a
   clean command, **Then** the robot leaves its dock and the command reports the confirmed new state.
5. **Given** the robot will not release its password locally, **When** setup detects this, **Then**
   it explains why and offers retrieval through the iRobot account instead.
6. **Given** discovery finds nothing (for example because a VPN owns the network route), **When** the
   user supplies a broadcast address or the robot's IP, **Then** setup finds or adds the robot.

---

### User Story 2 - See what every robot is doing, live (Priority: P1)

The owner, or a program, asks the kit for a robot's current state and gets the live picture:
battery, activity, mission details, bin or tank status, errors in plain language, signal strength
and lifetime counters. A consumer subscribes once and is pushed every meaningful change (mission
started, docked, stuck, bin full, connection lost) as it happens.

**Why this priority**: Automations, bots and apps are all driven by state and events. Without a
push stream every consumer would have to poll a robot that tolerates only one client.

**Independent Test**: With the service running, subscribe to events, start and dock a robot, and
confirm the expected sequence of events arrives with readable state.

**Acceptance Scenarios**:

1. **Given** a connected robot, **When** a consumer requests its state, **Then** the response
   includes battery, activity, current mission (identifier, start time, who started it),
   consumable/accessory status for that model, error state in readable form, signal strength,
   stored-map identity and lifetime counters.
2. **Given** a subscriber, **When** a robot starts, finishes, docks, gets stuck or reports a full
   bin, **Then** the subscriber receives a typed event for each within the latency target.
3. **Given** a robot that drops off the network, **When** connectivity is lost and later restored,
   **Then** subscribers receive connectivity events and the service reconnects without user action.
4. **Given** a robot reports an error or not-ready code, **When** state or events are read, **Then**
   the code is accompanied by a human-readable explanation.

---

### User Story 3 - Clean specific rooms by name (Priority: P2)

The owner tells a robot to clean "Laundry Room" then "Office", in that order. If iRobot account
credentials are configured, room names come from the robot's stored map. If not, the owner gives
names to room IDs the kit has learned, or cleans by room ID.

**Why this priority**: Room-targeted cleaning is the most-used capability beyond start and stop,
and the one where the vendor's routines failed the user.

**Independent Test**: Import rooms (or define a local room name), request a single-room clean, and
confirm from the robot's reported mission and travel that only that room was cleaned.

**Acceptance Scenarios**:

1. **Given** cloud credentials are configured, **When** rooms are imported, **Then** each room is
   listed with its ID and name, and unnamed rooms are shown with a clear placeholder.
2. **Given** a list of rooms in a chosen order, **When** a room clean is requested, **Then** the robot
   cleans those rooms in that order and the command confirms the mission started.
3. **Given** no cloud credentials, **When** a room clean is started by another client such as the
   iRobot app, **Then** the kit records the room IDs it sees so the owner can name them locally.
4. **Given** the robot's stored map has been re-created, **When** a room clean uses names from the
   older map, **Then** the kit refuses with a "map changed, re-import rooms" error rather than
   sending stale IDs.
5. **Given** a requested room name that matches nothing, **When** the command runs, **Then** it fails
   with a not-found error listing the valid rooms.

---

### User Story 4 - Build on the kit as a developer or an LLM agent (Priority: P2)

A developer writes a scheduler against the HTTP API, and an LLM agent with only the MCP server
answers "clean the kitchen with the mop and tell me when it's done". Both find the same
capabilities under the same names with the same errors, and neither needs to know how robots are
reached.

**Why this priority**: The kit exists to be built on. Automations, Hermes bots and a phone app are
the planned consumers.

**Independent Test**: Run the same scenario (list robots, room clean, wait for completion) through
the CLI, the HTTP API and the MCP server, and compare names, inputs, outputs and errors.

**Acceptance Scenarios**:

1. **Given** any capability in this spec, **When** it is invoked from the CLI, HTTP API or MCP,
   **Then** the capability name, parameters, result fields and error codes match across all three.
2. **Given** an HTTP request without valid credentials, **When** it reaches any non-health endpoint,
   **Then** it is rejected and nothing is executed.
3. **Given** an MCP client with no prior knowledge, **When** it lists available tools, **Then** each
   tool's description states what it does, its parameters, which models support it and whether it
   is verified on hardware.
4. **Given** an action a model does not support, **When** it is invoked on any surface, **Then** the
   result is a `not_supported` error naming the model and capability, and the robot is not
   contacted.
5. **Given** CLI output requested as machine-readable, **When** any command runs, **Then** output is
   structured data matching the HTTP API's response for the same capability.

---

### User Story 5 - Read and change robot settings (Priority: P2)

The owner switches a robot to two cleaning passes, turns edge cleaning off, sets the mop's
wetness, or pauses the robot's own built-in schedules so they can't fire alongside the owner's
automations. Each change is confirmed by reading it back from the robot.

**Why this priority**: These settings shape every run, and pausing robot-stored schedules is
needed so robots don't run from old vendor routines alongside new automations.

**Independent Test**: Change one setting, confirm it reads back changed, restore it, and confirm it
reads back restored.

**Acceptance Scenarios**:

1. **Given** a robot that supports a setting, **When** the owner changes it, **Then** the kit reports
   success only after the robot reports the new value.
2. **Given** a robot that accepts the write but does not apply it, **When** read-back does not
   match, **Then** the kit reports "not applied" with the value the robot actually holds.
3. **Given** a setting the model lacks (for example mop wetness on a vacuum, carpet boost on a model
   that ignores it), **When** it is requested, **Then** the result is `not_supported`.

---

### User Story 6 - Use cloud extras when available (Priority: P3)

With iRobot account credentials configured, the owner sees room names and floor plans, the cloud's
recent mission history, the path covered in past runs (with dirt and clutter detections), and a
live room-level mission timeline (heading to the laundry room, now in the living room, returning to
dock). With the cloud unreachable, all of this reports "unavailable" and nothing else changes.

**Why this priority**: Valuable extras, but optional by design. The kit must be complete without
them.

**Independent Test**: With cloud credentials, run a room clean and confirm timeline events arrive
during the run and coverage is retrievable afterwards. Then block the cloud and confirm local
capabilities still pass their acceptance tests.

**Acceptance Scenarios**:

1. **Given** cloud credentials, **When** a mission runs, **Then** room-level timeline events (travel
   to room, room entered, heading to dock, bin emptied, finished) are delivered as events within
   seconds of the robot reporting them to the cloud.
2. **Given** a completed mission, **When** its coverage is requested, **Then** the covered path and
   any dirt/clutter detections are returned once the cloud has published them, or "not yet
   available" before then.
3. **Given** the cloud is unreachable or credentials are rejected, **When** any cloud feature is
   requested, **Then** it returns `cloud_unavailable` with a reason, and local commands, state and
   events are unaffected.

---

### User Story 7 - Follow a robot's live position where the model allows it (Priority: P3)

For a model that reports its position, a consumer receives the robot's position (x, y, heading)
during a mission to draw a live map. For models that don't, the kit says so and offers the
room-level timeline when the cloud is configured.

**Why this priority**: Useful for maps and diagnostics, but only some models report position.

**Independent Test**: Run a mission on a position-capable model and confirm a stream of position
fixes. Request the same on a model without it and confirm `not_supported`.

**Acceptance Scenarios**:

1. **Given** a position-capable robot cleaning, **When** a consumer subscribes to position, **Then**
   it receives fixes at least every 2 seconds, each with timestamp and map identity.
2. **Given** a position-capable robot that has no fix (for example docked), **When** position is
   requested, **Then** the result says "no fix" rather than an error.
3. **Given** a model without position reporting, **When** position is requested, **Then** the result
   is `not_supported`.

---

### User Story 8 - Keep a local record of every mission (Priority: P3)

The kit records every mission and state transition it observes, so the owner can ask "what did the
robots do this month, and how did each run end" long after the robot's counters and the cloud's
roughly 30-mission window have rolled over.

**Why this priority**: History that isn't captured as it happens can't be recovered later, and the
robots keep only lifetime totals.

**Independent Test**: Run two missions, restart the service, and query history to confirm both are
present with start, end, outcome, rooms and initiator.

**Acceptance Scenarios**:

1. **Given** the service observed a mission, **When** history is queried, **Then** the mission
   appears with robot, start and end time, outcome (completed, cancelled, stuck, failed,
   manually docked), rooms requested, who started it, and duration.
2. **Given** history filters (robot, date range, outcome), **When** applied, **Then** only matching
   missions are returned.
3. **Given** the service restarts mid-mission, **When** it reconnects, **Then** the mission continues
   as the same record rather than being duplicated or lost.

---

### User Story 9 - Run the service on an always-on host (Priority: P3)

The owner moves the service from their laptop to an always-on machine on the home network. They
copy configuration (or supply it through environment settings), start it with one command, and
point their consumers at it.

**Why this priority**: Automations need a host that's always on, but local use comes first.

**Independent Test**: Deploy to a second machine using only the deployment guide, start it, and
issue a command from a consumer on another machine.

**Acceptance Scenarios**:

1. **Given** a supported always-on host, **When** the owner follows the deployment guide, **Then** the
   service runs there, survives a host reboot when configured to, and serves consumers on the
   network.
2. **Given** configuration supplied through environment settings, **When** the service starts,
   **Then** it uses those values in place of the per-user configuration location.
3. **Given** the service is running, **When** the owner runs the status command, **Then** it shows
   service health and each robot's connectivity.

---

### User Story 10 - Add support for another robot model (Priority: P3)

V1 ships profiles for only the two models in use. A developer with another model adds it later by
following the model-profile convention: declare the model's capabilities and any model-specific
mappings, run the hardware verification checklist, and mark each capability verified or
unverified, all without touching the CLI, HTTP API or MCP.

**Why this priority**: V1 covers two models, but the convention must make the next model routine
instead of a refactor.

**Independent Test**: Add a test model profile, confirm it appears across all surfaces with its
capabilities, and confirm undeclared actions return `not_supported`.

**Acceptance Scenarios**:

1. **Given** a new model declaration, **When** the service starts, **Then** robots of that model are
   recognised and their capabilities appear on all three surfaces.
2. **Given** a robot whose model has no profile, **When** setup discovers it, **Then** it is listed as
   unsupported with its model and SKU, and the owner is pointed to the add-a-model guide.

---

### Edge Cases

- **Another client holds the robot's single local connection** (iRobot app, Home Assistant, a
  script). The service reports the robot as `connection_held_elsewhere`, retries with backoff and
  documents how to release it, without crashing or affecting other robots.
- **Robot IP changes** (DHCP). The robot is re-found by its permanent ID and its address updated
  without re-running setup.
- **Robot leaves Wi-Fi coverage mid-mission.** Connectivity-lost and restored events fire. The
  mission isn't marked failed just for going silent; its outcome comes from the robot's state on
  reconnect.
- **Command accepted but state change arrives late.** The robot may register a pause or dock
  several seconds late (observed on the m6). The command waits up to a defined confirmation
  window, then returns `unconfirmed` with the current state, not a false success.
- **Conflicting commands from two consumers** (one starts, another docks): commands for one robot
  are applied in the order received, and each caller gets the outcome of its own command.
- **Robot search for dock exceeds the threshold.** Observed intermittently on the i5+, once for 70
  minutes. A `dock_search_timeout` alert event is emitted once per mission.
- **Cloud login succeeds but data is partial or stale** (new mission not yet published): the
  results say so explicitly.
- **Cloud credentials rejected, locked or expired**: cloud features go unavailable with the reason.
  The service backs off rather than retrying in a tight loop.
- **Robot holds no stored map, or its map version changes**: room features report `no_map` or
  `map_changed`, and imported room names are flagged stale.
- **Rooms with no name on the robot's map** (common on the m6): shown with a placeholder and
  nameable locally.
- **Service restarted while a robot is mid-mission**: state is re-read from the robot, and history
  continues the same mission.
- **Credential file with permissions broader than owner-only**: the service refuses to start and
  explains the fix.
- **Two robots with the same display name**: both remain addressable by ID, and name-based
  commands that are ambiguous fail with the candidates listed.
- **Any robot without a model profile found during discovery** (another Classic model, or a newer
  cloud-only model): listed as unsupported with model and SKU, not silently ignored and not
  controllable.

## Requirements *(mandatory)*

### Functional Requirements

**Setup & onboarding**

- **FR-001**: The kit MUST provide a guided setup that discovers iRobot robots on the local network
  and lists each one's display name, model, firmware version, network address, permanent robot ID
  and whether its model is supported. *(Origin: R6)*
- **FR-002**: Setup MUST accept a user-supplied broadcast address and MUST allow adding a robot by
  IP address when discovery cannot reach it. *(Origin: new — VPN default route blocked discovery
  in testing, session b10c0fbc)*
- **FR-003**: Setup MUST walk the user through local password retrieval with numbered,
  model-appropriate physical steps (robot on dock, hold HOME until it chimes, then confirm). It
  MUST keep retrying for a bounded window of at least 60 seconds and report a clear outcome:
  retrieved, timed out, or not available locally for this model. *(Origin: R6)*
- **FR-004**: Setup MUST offer optional iRobot account credentials. Before asking, it MUST state
  what they enable (room names, floor plans, cloud mission history, coverage, live mission
  timeline, password retrieval fallback) and that every local capability works without them.
  *(Origin: R2, R6; interview)*
- **FR-005**: When local password retrieval is unavailable for a robot and cloud credentials are
  configured, setup MUST offer to obtain that robot's password through the account. *(Origin: R6)*
- **FR-006**: Setup MUST verify every credential, robot password or account, by successfully
  connecting before saving it, and MUST report failures without saving. *(Origin: R6)*
- **FR-007**: Setup MUST be re-runnable to add or remove robots, rotate credentials, add or remove
  cloud credentials, and re-import rooms, without disturbing unrelated configuration.
  *(Origin: R6)*
- **FR-008**: When cloud credentials are configured, setup MUST import each robot's stored map
  rooms (IDs and names). *(Origin: R2)*

**Configuration & secrets**

- **FR-009**: All configuration and credentials MUST live in one documented per-user location
  outside any source repository. Credential storage MUST be readable by the owning user only.
  *(Origin: R7)*
- **FR-010**: Every configuration value, credentials included, MUST be overridable through
  environment settings for deployment. *(Origin: R7, R10)*
- **FR-011**: The kit MUST provide a configuration display that shows all settings with secrets
  redacted. *(Origin: R7, R8)*
- **FR-012**: Secrets MUST NOT appear in logs, CLI output, HTTP or MCP responses, event payloads or
  error messages. *(Origin: R8)*
- **FR-013**: The service MUST refuse to start if credential storage is readable by anyone other
  than the owner, and MUST say how to fix it. *(Origin: R8)*

**Service lifecycle & connections**

- **FR-014**: The kit MUST start the service with one command and stop it with one command, and MUST
  provide a status command showing service health and each robot's connection state. *(Origin: R9)*
- **FR-015**: The service MUST run in the foreground or as a background service, and MUST be
  installable to start automatically on host boot. *(Origin: R9, R10)*
- **FR-016**: The service MUST be the only holder of each robot's local connection. The CLI, HTTP API
  and MCP surfaces MUST act through the running service; the CLI MUST explain how to start the
  service if it isn't running. *(Origin: R3)*
- **FR-017**: The service MUST reconnect to robots automatically with backoff after any
  disconnection, and MUST detect and report the case where another client holds a robot's
  connection. *(Origin: R3)*
- **FR-018**: The service MUST re-locate a robot by its permanent ID when its network address
  changes. *(Origin: S3)*
- **FR-019**: Every local capability MUST work with the iRobot cloud unreachable or never
  configured. *(Origin: R1)*

**Robot inventory & capabilities**

- **FR-020**: Robots MUST be identified on every surface by their permanent robot ID. Display name and
  network address are attributes, never identifiers. Name-based addressing MUST be accepted only
  when unambiguous. *(Origin: S3)*
- **FR-021**: Each supported model MUST have a capability declaration listing what it supports. Each
  capability MUST carry a verification status: `verified`, meaning exercised on that model's
  hardware, or `unverified`. *(Origin: R4, R15)*
- **FR-022**: Listing robots MUST return, per robot: ID, display name, model and SKU, firmware, network
  address, connection state, and its capabilities with verification status. *(Origin: R4, R15)*
- **FR-023**: Invoking a capability the robot's model does not support MUST return `not_supported`
  naming the model and capability, without contacting the robot. *(Origin: R5)*
- **FR-024**: Robots of any model without a profile MUST appear in discovery and robot listings as
  `unsupported`, with model and SKU and a pointer to the add-a-model guide. They MUST NOT be
  controllable. *(Origin: interview)*
- **FR-025**: V1 MUST ship model profiles for exactly two models: the Roomba i5/i5+ (e.g. SKU
  i5G5020) and the Braava jet m6 (e.g. SKU m611020), including regional SKU variants of those
  models. Model profiles MUST follow a documented convention so further models can be added later
  as new profiles. *(Origin: interview)*

**Live state**

- **FR-026**: For each robot the kit MUST report on demand: battery level; activity (charging,
  cleaning, paused, returning to dock, emptying bin, stuck, idle); current mission (ID, start time,
  initiator: local, schedule, button, other app); last command received; error and not-ready
  codes, each with a readable explanation; Wi-Fi signal strength; stored map identity and version;
  robot-stored schedules (read-only); lifetime counters (missions completed, failed and cancelled;
  runtime hours; times stuck). *(Origin: new — capability inventory, session b10c0fbc)*
- **FR-027**: Where the model has them, the kit MUST also report: bin present and full; Clean Base
  presence and state; mop pad type detected; water tank present and level; lid open or closed.
  *(Origin: new — capability inventory)*

**Commands**

- **FR-028**: The kit MUST support these commands, subject to model capability: clean whole home;
  clean rooms (ordered list, by ID or name, with per-room options where supported); pause; resume;
  stop; return to dock; locate (play sound); empty bin into Clean Base; soft reboot; mapping run.
  *(Origin: new — capability inventory)*
- **FR-029**: Each command MUST confirm the robot's state actually changed. It MUST return one of:
  `confirmed` with the new state; `unconfirmed` with the current state once a per-command
  confirmation window expires; or a specific error. *(Origin: new — late pause registration
  observed, session b10c0fbc)*
- **FR-030**: Commands to one robot MUST be applied in the order received, and each caller MUST receive
  the outcome of its own command. *(Origin: R3)*
- **FR-031**: Room cleaning MUST reject requests whose room names or IDs belong to a different or
  outdated map version, with a `map_changed` error. It MUST NOT send an empty room list (which a
  robot would treat as "whole home"). *(Origin: new — protocol behaviour documented in roombapy)*
- **FR-032**: Without cloud credentials, the kit MUST learn room IDs from room cleans it observes
  (including ones started by other clients) and let the owner assign local names to them.
  *(Origin: R1, R2)*

**Settings**

- **FR-033**: The kit MUST read and, where the model supports it, write: cleaning passes (automatic,
  one, two); edge cleaning; always-finish (keep cleaning when the bin is full); child lock; eco
  charging; robot-stored schedule hold; mop pad wetness; mop spray overlap (standard, deep,
  extended). *(Origin: new — capability inventory)*
- **FR-034**: A setting change MUST be reported as applied only after the robot reports the new
  value. A mismatch MUST be reported as `not_applied`, together with the value the robot actually
  holds. *(Origin: new — silent write drops observed by Roomba+ on i3)*
- **FR-035**: Where a model needs related settings written together (e.g. the two values behind the
  passes setting), the kit MUST handle it so callers set one logical setting. *(Origin: new —
  Roomba+ field finding)*

**Events & alerts**

- **FR-036**: The kit MUST deliver a push stream of typed events to subscribers on the HTTP API and
  to MCP clients. Event types: state changed, mission started, mission finished (with outcome),
  docked, stuck, bin full, error raised/cleared, connectivity changed, setting changed, cloud
  timeline event, position fix, alert. *(Origin: R11)*
- **FR-037**: Every event MUST carry a schema version, event type, robot ID, timestamp and
  type-specific payload. Subscribers MUST be able to filter by robot and event type. *(Origin: S1)*
- **FR-038**: The kit MUST emit a `dock_search_timeout` alert when a robot has been returning to dock
  longer than a configurable threshold (default 5 minutes), once per mission. *(Origin: new —
  intermittent docking failure, session b10c0fbc)*

**Live position**

- **FR-039**: For models that report position, the kit MUST provide current position on request
  and a position stream during missions (x, y, heading, timestamp, map identity). It MUST
  distinguish "no fix" from "not supported". *(Origin: new — m6 verified, i5+ verified absent)*
- **FR-040**: The kit MUST stop requesting position from a robot that has never answered, and MUST NOT
  let position polling degrade command or state responsiveness. *(Origin: new)*

**Cloud features (optional)**

- **FR-041**: With cloud credentials configured, the kit MUST provide: stored-map rooms and floor plan
  per robot; cloud mission history; per-mission coverage path with dirt and clutter detections;
  and live mission timeline events with room-level location. *(Origin: R2)*
- **FR-042**: Every cloud-backed capability MUST return `cloud_unavailable` with a reason when the cloud
  is unreachable, credentials are missing or rejected, or data is not yet published. It MUST NOT
  block, slow or fail any local capability. *(Origin: R1, R2)*
- **FR-043**: The kit MUST cache the last successfully imported rooms and floor plans so they remain
  usable while the cloud is unavailable, marked with their retrieval time. *(Origin: R1)*

**Local history**

- **FR-044**: The kit MUST record every mission it observes (robot, start and end, outcome, rooms,
  initiator, duration, errors during the mission) and every activity transition, persisting across
  service restarts. *(Origin: S2)*
- **FR-045**: History MUST be queryable on all surfaces by robot, date range and outcome. *(Origin: S2)*

**Surfaces & contracts**

- **FR-046**: Every capability in FR-001 to FR-045 MUST be available through the CLI, the HTTP API and
  the MCP server, with the same capability names, parameters, result fields and error codes.
  *(Origin: R13; constitution III)*
- **FR-047**: The CLI MUST offer human-readable output and machine-readable structured output that
  matches the HTTP API's response for the same capability. *(Origin: R13)*
- **FR-048**: The HTTP API MUST require authentication for every endpoint except a health check. It MUST
  carry its contract version in every route, and MUST publish a machine-readable description of
  all endpoints. *(Origin: S1, S5)*
- **FR-049**: The MCP server MUST describe each tool's purpose, parameters, supporting models and
  verification status. It MUST be usable both by local clients and by authenticated clients over
  the network. *(Origin: R13)*
- **FR-050**: The kit MUST use one shared catalogue of error codes across surfaces, including at least:
  `not_supported`, `robot_offline`, `connection_held_elsewhere`, `unconfirmed`, `not_applied`,
  `map_changed`, `room_not_found`, `ambiguous_name`, `cloud_unavailable`, `unauthorized`,
  `invalid_request`. *(Origin: R5, R13)*

**Documentation**

- **FR-051**: The kit MUST ship with:
  - a getting-started walkthrough from zero to a first successful command;
  - a setup guide covering robot passwords and optional cloud credentials;
  - a reference for every capability with CLI, HTTP and MCP examples;
  - a support matrix of models and capabilities with verification status;
  - an event reference;
  - a deployment guide;
  - troubleshooting for connection conflicts with other clients, discovery under VPNs and
    docking alerts;
  - developer guides for building consumers and adding a model.
  *(Origin: R12, R13; constitution IX)*

### Key Entities

- **Robot**: one physical device. Permanent ID, display name, model, firmware, network address,
  connection state, owning credentials reference.
- **Model profile**: what a model can do. SKU pattern, capability declarations with verification
  status, model-specific setting mappings.
- **Capability**: a named action, setting or data feed (e.g. `clean_rooms`, `edge_cleaning`,
  `live_position`), each with verification status per model.
- **Credential**: a robot password, iRobot account credentials, or an API access token. Never
  exposed.
- **Map and Room**: a robot's stored map (ID, version) and its rooms (ID, imported name, local name,
  last seen). Belongs to one robot and one map version.
- **Mission**: one cleaning run. Robot, start and end, initiator, requested rooms, outcome, duration,
  related events, optional cloud coverage reference.
- **State snapshot**: a robot's current reported condition.
- **Event**: a typed, versioned change notification with robot ID, timestamp and payload.
- **Alert**: an event raised by the kit's own rules (e.g. dock search timeout), with threshold
  configuration.
- **Position fix**: x, y, heading, timestamp and map identity, for capable models.
- **Coverage record**: a completed mission's covered path, dirt and clutter detections. Cloud-sourced.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A new user with a supported robot on their Wi-Fi reaches a first successful clean
  command in under 15 minutes, using only the getting-started guide.
- **SC-002**: 100% of capabilities are available on all three surfaces with identical names,
  parameters, results and error codes, as checked by a parity test over the full capability list.
- **SC-003**: 95% of commands return a confirmed outcome within 10 seconds of being issued. The rest
  return `unconfirmed` or a specific error, never a false success.
- **SC-004**: With the iRobot cloud blocked, 100% of local-capability acceptance scenarios pass.
- **SC-005**: Subscribers receive local events within 2 seconds of the robot reporting the change.
- **SC-006**: After a robot or network outage, the service restores the robot connection within
  60 seconds of the robot becoming reachable, with no user action.
- **SC-007**: A scan of all logs, CLI output, HTTP and MCP responses and events produced during the
  acceptance suite finds zero credentials.
- **SC-008**: An LLM agent given only the MCP server completes "clean <room> on <robot> and report
  when it's done" end to end without human help.
- **SC-009**: The service is ready to accept commands within 10 seconds of the start command on a
  supported host.
- **SC-010**: Every capability marked `verified` cites a hardware run. At release, all
  capabilities listed as verified in Assumptions are re-verified on the i5+ and m6.
- **SC-011**: Adding a model profile requires no changes to the CLI, HTTP API or MCP layers.

## Assumptions

- Robots are already joined to Wi-Fi through the iRobot app. Provisioning is not part of V1.
- The service host is on the same local network as the robots. Discovery relies on broadcast
  reaching them; otherwise robots are added by IP.
- One household, one owner. Consumers authenticate with API tokens; there are no per-user roles.
- Supported hosts are macOS and Linux. A container image is the deployment path for other
  always-on hosts.
- By default the HTTP API and network MCP listen only on the local machine. Exposing them on the
  home network is opt-in and always authenticated.
- Local history is kept indefinitely by default, with configurable pruning.
- The dock-search alert threshold defaults to 5 minutes.
- Cloud features rely on an unofficial vendor interface that may change without notice. Its
  behaviour is documented as observed, not guaranteed.
- Initially `verified` (from session b10c0fbc hardware runs):
  - Both robots: discovery; local password retrieval; start, pause, resume, dock and locate;
    state reads.
  - Roomba i5+ only: empty bin into Clean Base; room cleaning; cloud rooms, history, coverage
    and live timeline. The i5+ is verified to lack live position.
  - Braava jet m6 only: live position.
- Initially `unverified`:
  - All setting writes.
  - Robot-stored schedule hold.
  - On the m6: room cleaning and the cloud timeline.
  - The mapping run and soft reboot.
- English-only user-facing text.

## Non-Goals

- Scheduling or automation engine. Consumers build this on the kit; V1 only reads robot-stored
  schedules and can hold them.
- Writing or editing robot-stored schedules.
- Hermes Agent integration, phone app or any graphical UI. These are separate projects on top of
  the kit.
- Map creation or editing (splitting or merging rooms, naming rooms on the robot, keep-out zones).
  These stay in the iRobot app.
- Firmware modification or rooting.
- Wi-Fi provisioning of new robots.
- Live coordinates for models whose firmware doesn't report them, including through traffic
  interception or firmware analysis.
- Raw protocol capture mode for reverse-engineering new models (ideation S6), deferred.
- Profiles for any model other than the Roomba i5/i5+ and Braava jet m6, including the 2025
  cloud-first generation (Roomba 105/205, Combo 405, Max 705). V1 provides the convention for
  adding them, not the profiles.
- Multi-household or multi-user account management.
- Matter control.

## Decisions

| Ref | Decision | Decided by | Date |
| --- | --- | --- | --- |
| FR-019, FR-041, FR-042 | Use the iRobot cloud wherever it adds value, but never as a dependency for any local capability; rejected: local-only, and cloud-required | Hayden | 2026-11-08 |
| FR-004 | Setup must make clear that cloud credentials are optional and what they add, and explain how robot passwords are obtained | Hayden | 2026-11-08 |
| FR-025, FR-024 | V1 supports exactly the two models in use (Roomba i5/i5+, Braava jet m6) and establishes conventions for adding more later; other models are listed as unsupported. Rejected: broad Classic-family support, and the 2025 cloud-first generation | Hayden | 2026-11-08 |
| FR-009, FR-010 | Standard secrets location outside the repo, usable by any installer | Hayden | 2026-11-08 |
| FR-014 | One command each to start and stop the service | Hayden | 2026-11-08 |
| FR-016 | Long-running service that owns robot connections, with all surfaces as its clients (shape delegated to Claude; forced by the one-connection limit) | Claude (delegated by Hayden) | 2026-11-08 |
| FR-046 | Three surfaces: CLI, HTTP API and MCP server (frameworks fixed in the constitution) | Hayden | 2026-11-08 |
| Non-Goals | Automations, Hermes integration and the phone app are built on top of the kit, not inside it | Hayden | 2026-11-08 |
