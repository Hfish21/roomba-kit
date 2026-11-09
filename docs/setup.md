# Setup

`roomba-kit setup` is a guided wizard over the operations on this page. Each step is also a
command of its own, so you can script it or run it over HTTP or MCP. The wizard adds prompts and
explanations, but it does nothing those commands can't.

## Re-running setup

When at least one robot is configured, `roomba-kit setup` opens a menu:

1. Add a robot
2. Re-pair a robot (retrieve or set its password)
3. Add, change or remove iRobot account credentials
4. Re-import rooms
5. Remove a robot
6. Done

Each option changes only its own entries in `config.toml` and `credentials.toml`. The other robots,
the account and your settings are left alone.

## Finding robots

```bash
roomba-kit discover                         # broadcast on 255.255.255.255
roomba-kit discover --broadcast 192.0.2.255 # your subnet's broadcast address
```

Each robot is listed with its ID, name, SKU, firmware, address, whether its model is supported, and
whether it's already configured. Robots of an unsupported model can be added and listed, with
`supported: no`, but they can't be commanded. See [Adding a model](guides/adding-a-model.md).

**Behind a VPN**, discovery often finds nothing, because the VPN owns the default route. Use your
subnet's broadcast address, which is your router's address with the last number replaced by 255.
For example, `192.0.2.255` (a documentation address; yours will differ). To make the kit's own rediscovery use
it too (the kit rediscovers a robot by its ID when the robot's IP changes), set this in
`config.toml`:

```toml
[discovery]
broadcast = "192.0.2.255"
```

**By IP address.** If discovery still can't reach the robot, add it directly. Its IP is in your
router's client list:

```bash
roomba-kit robots add 192.0.2.10
```

The robot must answer at that address. This adds it without a password. Pair it next.

Docker Desktop for macOS can't receive the discovery broadcast, so add robots by IP there.

## Pairing: the robot's local password

The kit controls each robot over its local connection, which needs that robot's password. The
robot releases it over the network only while you hold its HOME button:

| Model | Steps |
|---|---|
| Roomba i5/i5+ | 1. Place the robot on its dock (on the Clean Base for the i5+). 2. Press and hold **HOME** about 2 s, until it chimes. 3. Release. |
| Braava jet m6 | 1. Place the robot on its dock. 2. Press and hold **HOME** about 2 s, until it chimes. 3. Release. |

```bash
roomba-kit robots password Vacuum        # retrieve: start this, then hold HOME
roomba-kit robots password Vacuum --manual   # type a password you already know (hidden prompt)
```

Retrieval retries every 2 seconds for 60 seconds (`--timeout-s` changes the window). It ends in
one of these outcomes:

| Outcome | Meaning | What to do |
|---|---|---|
| `stored` | Retrieved, verified by connecting, and saved | Nothing. The robot connects. |
| `cloud_only` | The robot answered but released no password | Retry and hold HOME until it chimes. Some robots release it only to the iRobot account: `roomba-kit cloud password Vacuum`. |
| `timeout` | The robot never answered | Check that it's docked, awake and on the same network. |

Every password, retrieved or typed, is checked by connecting with it before it's saved. If the robot
rejects it or can't be reached, nothing is saved, and the previous password, if there was one,
stays in place. Passwords are never printed, returned by the API or written to logs.

!!! note
    The robot accepts one local connection at a time. Close the iRobot app before pairing, or
    the check fails with `connection_held_elsewhere`.

## Cloud credentials

iRobot account credentials are **optional**. Every local feature works without them: cleaning,
docking, state, settings and events. They add:

- room names and IDs from the robot's stored map, and floor plans;
- the cloud mission history and coverage maps;
- the live, room-by-room mission timeline;
- password retrieval for robots that won't release theirs locally.

```bash
roomba-kit cloud login --username you@example.com --country US   # prompts for the password
roomba-kit cloud status
roomba-kit cloud password Vacuum   # a robot's local password, via the account
roomba-kit cloud logout            # removes the stored credentials
```

The login is checked by logging in before it's saved. After that, the service keeps one login in
the background. `cloud status` reports `connected`, `not_configured`, or `unavailable` with a
reason, such as `auth_failed`, `timeout` or `connection_failed`. When the cloud is unreachable,
the kit retries with a backoff of up to 15 minutes, and local control is never delayed. Rejected
credentials aren't retried until you change them, because repeated bad logins can lock the
account. To turn the cloud off entirely, set `[cloud] enabled = false` in `config.toml`.

## Where things are stored

`roomba-kit config path` prints both directories:

| File | Holds |
|---|---|
| `<config>/config.toml` | Robots (ID, name, address, model), server, discovery and cloud settings. No secrets. |
| `<config>/credentials.toml` | Robot passwords, the account login and hashed API tokens. Owner-only (mode 0600). The service refuses to start if anyone else can read it. |
| `<state>/cli.token` | This machine's `cli` API token, created by setup |

By default, `<config>` is `~/.config/roomba-kit` and `<state>` is `~/.local/state/roomba-kit`.
`roomba-kit config show` prints the effective configuration, with every secret shown as `"***"`.
Every value can be overridden by environment variables, such as `ROOMBA_KIT_CLOUD__PASSWORD` and
`ROOMBA_KIT_ROBOTS__<ID>__PASSWORD`, and those values are never written back to the files.

## Removing a robot

```bash
roomba-kit robots remove Mop
```

This stops the robot's connection and deletes its entries from `config.toml` and
`credentials.toml`. Nothing else changes. To use the robot again, add and pair it again.

## The same operations over HTTP

| Operation | HTTP |
|---|---|
| `discover_robots` | `GET /v1/discover_robots?broadcast=192.0.2.255` |
| `add_robot` | `POST /v1/robots` `{"address": "192.0.2.10"}` |
| `retrieve_password` | `POST /v1/robots/{robot}/password/retrieve` `{"timeout_s": 60}` |
| `set_robot_password` | `POST /v1/robots/{robot}/password` `{"password": "…"}` (not on MCP) |
| `cloud_login` | `POST /v1/cloud_login` `{"username", "password", "country"}` (not on MCP) |
| `cloud_retrieve_password` | `POST /v1/robots/{robot}/password/cloud` |
| `cloud_status` / `cloud_logout` | `GET /v1/cloud_status` · `POST /v1/cloud_logout` |
| `remove_robot` | `DELETE /v1/robots/{robot}` |

The two operations that take a secret, `set_robot_password` and `cloud_login`, aren't exposed
over MCP, so passwords never pass through an LLM's context. The full list is in the
[operations reference](reference/operations.md).
