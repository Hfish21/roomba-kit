# Contract: Surface Mapping (v1)

How each operation in `operations.md` appears on each surface. These rules are applied by the
generators, so no surface is written by hand.

## HTTP API

- Base path `/v1`. Auth is `Authorization: Bearer <token>` on everything except `GET /v1/health`.
- Bind defaults to `127.0.0.1:8765`. Setting `server.host` exposes it on the LAN.
- OpenAPI is served at `/v1/openapi.json`, with docs at `/v1/docs`.
- Route derivation, from registry metadata:
  - System: `/v1/<operation>`. Reads are GET; mutations are POST, with a JSON body.
  - Robot-scoped reads: `GET /v1/robots/{robot}/<noun>` (`state`, `capabilities`, `settings`,
    `rooms`, `position`, `floor-plan`, `cloud-history`, `missions/{id}/coverage`).
  - Commands: `POST /v1/robots/{robot}/commands/<command>`, with a JSON body of params.
  - Settings: `PUT /v1/robots/{robot}/settings/{setting}`, body `{value}`.
  - Events: `GET /v1/events` (JSON page, or SSE when `Accept: text/event-stream`),
    `GET /v1/events/wait`, `GET /v1/robots/{robot}/missions/wait`.
- Errors: `{"error": {"code", "message", "robot_id"?, "details"?}}`, with the HTTP status from
  `errors.md`.
- MCP is mounted at `/mcp` (streamable HTTP), behind the same bearer auth.

## MCP

- Server name `roomba-kit`. Tool name = operation name.
- Each tool description is generated from the registry and contains:
  - a one-line purpose;
  - parameter docs;
  - "Supported on: <profiles>";
  - "Verification: <per profile>";
  - for motion commands, a note that the robot will physically move.
- Tool annotations: read-only operations set `readOnlyHint`. Motion commands set
  `destructiveHint=false` and `idempotentHint=false`.
- Tool errors are returned as MCP tool errors whose text starts with the error `code`.
- Transports:
  - **HTTP** (in-service, `/mcp`) for remote agents.
  - **stdio** (`roomba-kit mcp`) for local clients. It proxies to the service API using the
    CLI's token. If the service is down, it returns `service_unavailable` on every call.
- Excluded from MCP: `set_robot_password`, `cloud_login` (secrets in requests) and the
  `token_*` operations.

## CLI

- Entry point `roomba-kit`. The global options are:

  | Option | Meaning |
  |---|---|
  | `--json` | Machine output; byte-identical to the HTTP JSON body |
  | `--service-url` | Default `http://127.0.0.1:8765` |
  | `--quiet` | Suppress non-essential output |

- Command tree:

  | Area | Commands |
  |---|---|
  | Lifecycle (local; work without the service) | `serve`, `service start`, `service stop`, `service status`, `service install`, `service uninstall`, `service logs`, `mcp`, `config path`, `token create`, `token list`, `token revoke`, `version` |
  | Setup | `setup` (wizard), `discover [--broadcast ADDR]`, `robots add ADDR`, `robots remove ROBOT`, `robots password ROBOT [--manual]`, `robots set-password ROBOT`, `cloud login`, `cloud logout`, `cloud status`, `cloud password ROBOT` |
  | Inventory | `health`, `robots list`, `robots show ROBOT`, `state ROBOT`, `capabilities ROBOT` |
  | Commands | `clean ROBOT [--room NAME_OR_ID …] [--unordered]`, `pause ROBOT`, `resume ROBOT`, `stop ROBOT`, `dock ROBOT`, `find ROBOT`, `evac ROBOT`, `reboot ROBOT`, `train ROBOT` |
  | Settings | `settings show ROBOT`, `settings set ROBOT SETTING VALUE` |
  | Rooms | `rooms list ROBOT`, `rooms import ROBOT`, `rooms name ROBOT ROOM_ID NAME`, `rooms clean ROBOT --rooms NAME_OR_ID …`, `floor-plan ROBOT` |
  | Position | `position ROBOT [--watch]` |
  | Events & history | `events list`, `events watch [--robot] [--type …]`, `events wait --since SEQ [--robot] [--types …]`, `missions list`, `missions show ID`, `missions wait ROBOT`, `cloud history ROBOT`, `coverage ROBOT MISSION` |
  | Config & verification | `config show`, `verify ROBOT [CAPABILITY …] [--allow-motion]` |

- `clean ROBOT` with no `--room` maps to `clean`; with rooms it maps to `clean_rooms`. This is
  a presentation alias and the only CLI name that differs from the operation name.
- Exit codes come from `errors.md`, with 0 on success. `unconfirmed` exits 3.
- If the service isn't running, service-backed commands print
  `Service not running — start it with: roomba-kit service start` and exit 69.
