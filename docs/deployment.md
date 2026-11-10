# Deployment

The kit is one long-running service that owns every robot's local connection (each robot accepts
only one). Run it wherever is always on and on the robots' LAN. Pick one of:

| Host | How | Starts |
|---|---|---|
| Your laptop, ad hoc | `roomba-kit service start` / `stop` | When you run it |
| A Mac that stays logged in | `roomba-kit service install` (launchd agent) | At login |
| A Linux box (Pi, NUC, server) | `roomba-kit service install` (systemd user unit) | At boot, with linger |
| A Linux box with Docker | `deploy/compose.yaml` | At boot (`restart: unless-stopped`) |

Run exactly one of these per robot set. Two services fight over the robots' single connection.

## Start on login or boot: `service install`

Install the kit with a stable interpreter first, for example `uv tool install roomba-kit` (or a
checkout's `uv run`). Don't use `uvx`: the installed unit points at the current Python, and
`uvx`'s cached environment can be deleted under it.

```bash
roomba-kit service install --print   # show what would be written; changes nothing
roomba-kit service install           # write it, start it, wait for /v1/health
roomba-kit service uninstall         # stop it and remove it
```

The unit runs `<this python> -m roomba_kit.cli.main serve`. It pins `ROOMBA_KIT_CONFIG_DIR` and
`ROOMBA_KIT_STATE_DIR` to the directories `roomba-kit config path` shows at install time, so
re-run `service install` if you move them. Output goes to `<state>/service.log`, so
`roomba-kit service logs` works the same as with `service start`. If a `service start` process is
running, `install` refuses until you `roomba-kit service stop` it.

The supervisor owns the process, not the pidfile, so `service status` shows `pid: null` while
`health` is ok. To stop it, use `service uninstall` (or `launchctl bootout` / `systemctl --user
stop`); killing the process just gets it restarted.

Never put a secret in the unit's environment: the plist and unit file are world-readable. Secrets
belong in `credentials.toml` (0600) or the container's env file.

### macOS (launchd)

`service install` writes `~/Library/LaunchAgents/com.roomba-kit.service.plist` (`RunAtLoad`,
`KeepAlive`) and loads it with `launchctl bootstrap gui/<uid>`. A LaunchAgent runs in your login
session, so it starts **at login, not at boot**. For an always-on Mac, turn on automatic login.

If the service can't reach robots once it runs under launchd but can from a terminal, check
**System Settings → Privacy & Security → Local Network** and allow the Python it runs.

### Linux (systemd)

`service install` writes `~/.config/systemd/user/roomba-kit.service` (`Restart=on-failure`),
then runs `systemctl --user daemon-reload` and `enable --now`. A user unit only runs while you
have a session unless lingering is on, which `install` reminds you about:

```bash
loginctl enable-linger "$USER"   # start at boot, without logging in
```

On a host without systemd (some containers, WSL without systemd), `install` exits with
`not_supported`; run `roomba-kit serve` under your own supervisor instead.

## Container

```bash
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml exec roomba-kit roomba-kit token create laptop
roomba-kit --service-url http://<host>:8765 robots list   # with ROOMBA_KIT_API_TOKEN set
```

The image (`deploy/Dockerfile`) is `python:3.13-slim` with the locked dependencies, runs as the
non-root user `roomba` (uid/gid 1000), and sets:

| Variable | Value |
|---|---|
| `ROOMBA_KIT_CONFIG_DIR` | `/config` (volume) |
| `ROOMBA_KIT_STATE_DIR` | `/state` (volume) |
| `ROOMBA_KIT_SERVER__HOST` | `0.0.0.0` |

Its `HEALTHCHECK` calls `GET /v1/health` with Python (slim has no curl), on whatever port
`server.port` resolves to.

**Host networking is required.** Discovery finds robots by UDP broadcast, which doesn't cross a
bridge network, so compose uses `network_mode: host`. That also means `ports:` does nothing: the
service binds the host's port 8765 (change it with `ROOMBA_KIT_SERVER__PORT`).

**Docker Desktop on macOS can't discover robots**: its "host" network is a Linux VM's, not your
Mac's LAN. Add robots by IP (`roomba-kit robots add 192.0.2.10`) instead, or on a Mac just use
`service install`.

### Volumes and file permissions

Compose uses named volumes (`config`, `state`), which Docker creates owned by `roomba`. To use
bind mounts instead, the host directories must be writable by uid 1000 (or set `user:` in
compose to your uid), and a mounted `credentials.toml` must be mode `0600` and owned by that
uid. The service refuses to read a credentials file anyone else can read, and a 0600 file owned
by a different uid is unreadable to it:

```bash
chmod 600 ./config/credentials.toml && sudo chown 1000:1000 ./config/credentials.toml
```

## Configuration from the environment

Every `config.toml` setting can come from `ROOMBA_KIT_*` variables (`__` nests), and env wins
over the files. Values from env are never written back to disk. The robot list can also come from
`ROOMBA_KIT_ROBOTS`, a JSON list merged by `id` over the robots in `config.toml` (an entry
overrides only the fields it sets; unknown ids are added):

```bash
ROOMBA_KIT_ROBOTS='[{"id": "<BLID>", "name": "Vacuum", "address": "192.0.2.10", "profile": "roomba_i5"}]'
```

`robots add` / `setup` still write `config.toml` through the API, so keep the config dir
writable unless every robot comes from env. Removing an env-defined robot lasts only until the
service restarts; take it out of `ROOMBA_KIT_ROBOTS` too.

For a container that runs from env alone, set `ROOMBA_KIT_API_TOKEN` in the service's
environment: the service accepts it as a bearer token (named `env`; at least 16 characters, or
the service refuses to start), so one secret serves both the service and the CLI inside the
container. Make one with `python3 -c "import secrets; print('rk_' + secrets.token_urlsafe(32))"`.

| Variable | Sets |
|---|---|
| `ROOMBA_KIT_SERVER__HOST`, `ROOMBA_KIT_SERVER__PORT` | Bind address and port |
| `ROOMBA_KIT_DISCOVERY__BROADCAST` | Rediscovery broadcast address (e.g. `192.0.2.255`) |
| `ROOMBA_KIT_ROBOTS` | Robots as JSON `[{id, name, address, profile}]`, merged by `id` over `config.toml` |
| `ROOMBA_KIT_ROBOTS__<BLID>__PASSWORD` | One robot's local password |
| `ROOMBA_KIT_CLOUD__USERNAME`, `ROOMBA_KIT_CLOUD__PASSWORD` | iRobot account, for cloud extras |
| `ROOMBA_KIT_CONFIG_DIR`, `ROOMBA_KIT_STATE_DIR` | Where the files live |
| `ROOMBA_KIT_API_TOKEN` | The token the CLI and stdio MCP send; the service also accepts it (as `env`) |

For the container, put secrets in `deploy/roomba-kit.env`, one `NAME=value` per line. Compose
reads it if it exists; `*.env` is gitignored and never copied into the image. Treat that file
like `credentials.toml`: `chmod 600` it.

## Exposing the service on the LAN

By default the service binds `127.0.0.1`. To reach it from other machines, set `server.host`
(`0.0.0.0`, or one interface's address) in `config.toml` or via `ROOMBA_KIT_SERVER__HOST`; the
container already does. Every route except `GET /v1/health` needs a bearer token, so give each
client its own and revoke it when that client goes away:

```bash
roomba-kit token create phone        # prints the token once; only its hash is stored
roomba-kit token list
roomba-kit token revoke phone
```

Clients send `Authorization: Bearer <token>`; the CLI on another machine reads it from
`ROOMBA_KIT_API_TOKEN` and takes `--service-url http://<host>:8765` (or `ROOMBA_KIT_SERVICE_URL`).
Traffic is plain HTTP, so keep it on a network you trust (or put a TLS proxy in front); don't
forward the port to the internet.

## Backups

Everything the kit keeps is in two directories. Stop the service (so `state.db` is consistent),
copy both, start it again:

| Dir | Holds | Lose it and… |
|---|---|---|
| config | `config.toml` (robots, settings), `credentials.toml` (passwords, token hashes) | re-run setup and re-issue tokens |
| state | `state.db` (missions, events, rooms), `cli.token`, `service.log` | lose history and learned rooms |

```bash
roomba-kit config path                       # where they are on this host
docker run --rm -v roomba-kit_config:/config -v roomba-kit_state:/state -v "$PWD":/backup \
  python:3.13-slim tar czf /backup/roomba-kit-backup.tgz /config /state   # container volumes
```

A backup contains secrets: store it like one, and restore `credentials.toml` as 0600.
