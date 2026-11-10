# Contract: Configuration & Secrets (v1)

## Locations (research R5)

| What | Default | Override |
|---|---|---|
| Config dir | `$XDG_CONFIG_HOME/roomba-kit` or `~/.config/roomba-kit` | `ROOMBA_KIT_CONFIG_DIR` |
| State dir (db, pidfile, logs) | `$XDG_STATE_HOME/roomba-kit` or `~/.local/state/roomba-kit` | `ROOMBA_KIT_STATE_DIR` |

`roomba-kit config path` prints both.

## `config.toml` (no secrets; safe to share in bug reports)

```toml
[server]
host = "127.0.0.1"
port = 8765

[alerts]
dock_search_minutes = 5

[history]
event_retention_days = 30   # missions are kept indefinitely

[discovery]
broadcast = "255.255.255.255"   # rediscovery address; use the subnet's (e.g. 192.0.2.255) under a VPN

[[robots]]
id = "<blid>"
name = "Kitchen vacuum"
address = "192.0.2.10"      # hint; refreshed by rediscovery
profile = "roomba_i5"
sku = "i5G5020"             # from discovery; informational
firmware = "daredevil+2.6.0+…"

[cloud]
enabled = true
country = "US"
```

## `credentials.toml` (mode 0600, enforced)

```toml
[robots."<blid>"]
password = "..."

[cloud]
username = "..."
password = "..."

[[tokens]]
name = "cli"
sha256 = "..."
created_at = "2026-11-08T20:00:00Z"
```

## Environment overrides

These use pydantic-settings, with `__` as the nesting delimiter.

| Variable | Overrides |
|---|---|
| `ROOMBA_KIT_SERVER__HOST`, `ROOMBA_KIT_SERVER__PORT` | Server bind address and port |
| `ROOMBA_KIT_CLOUD__USERNAME`, `ROOMBA_KIT_CLOUD__PASSWORD` | Cloud credentials |
| `ROOMBA_KIT_ROBOTS` | The robot list: a JSON list of `{id, name, address, profile}` merged by `id` over `config.toml` (an entry overrides only the fields it sets; new ids are added). Invalid JSON or entries → `invalid_request` |
| `ROOMBA_KIT_ROBOTS__<BLID>__PASSWORD` | One robot's password |
| `ROOMBA_KIT_API_TOKEN` | Token the CLI and stdio MCP send (otherwise read from the `cli` token file). The service also accepts it as a bearer token named `env` (≥16 characters, else the service refuses to start), so a container can run from env alone (FR-010) |

Precedence: env, then the files, then defaults. Values coming from env are never written back to
disk: `save_config` only takes the file's own content, so env robots stay out of `config.toml`.

## Rules

- The service refuses to start if `credentials.toml` mode is broader than 0600 (FR-013), and
  prints the `chmod` fix.
- The service re-reads `credentials.toml` when its mtime changes, so tokens created by the CLI
  take effect without a restart.
- A logging filter redacts every known secret value and the patterns `password=`, `token` and
  `Authorization` (FR-012).
- The plaintext `cli` token is stored at `<config>/cli.token` (0600), next to
  `credentials.toml`, so every credential lives in the config dir (FR-009) and the local CLI can
  authenticate. A broader mode gets the same refusal as `credentials.toml`: the CLI won't read
  it and the service won't start (FR-013).
- Earlier builds kept it at `<state>/cli.token`. The first lookup on a new build copies it to
  `<config>` (keeping its mode) unless a token is already there, and leaves the old file so an
  older CLI or service on the same machine keeps working. The old copy is deleted the next time
  the `cli` token is created or revoked, since it would then be stale.
