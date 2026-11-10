# Troubleshooting

Each problem below starts with what the kit prints. The CLI prints errors on stderr as
`Error (<code>): <message>`, and with `--json` it prints the HTTP body instead,
`{"error": {"code", "message", "details"?}}`. The exit codes and HTTP statuses for every code are
in the [errors reference](reference/errors.md).

Two commands answer most questions:

```bash
roomba-kit robots list      # the `connection` column: connected, connecting, offline,
                            # held_elsewhere, auth_failed, not_configured
roomba-kit service logs -n 100
```

## The service isn't running (exit 69)

**Symptom.** Any robot command prints this and exits 69 (`service_unavailable`):

```text
Service not running — start it with: roomba-kit service start
```

**Cause.** Every robot command goes through the service, because the service owns each robot's
single local connection. The CLI couldn't reach it at `--service-url` (default
`http://127.0.0.1:8765`, or `ROOMBA_KIT_SERVICE_URL`).

**Fix.**

```bash
roomba-kit service status
roomba-kit service start     # waits for /v1/health; logs go to <state>/service.log
roomba-kit health
```

If `service start` fails, read `roomba-kit service logs`: the usual cause is a credentials file
with the wrong mode ([below](#file-permission-errors)) or the port already in use. If the service
runs on another machine, pass `--service-url http://<host>:8765`. If you installed it with
`service install`, the supervisor owns it, so `service status` shows `pid: null` while `health`
is ok. See [Deployment](deployment.md).

## 401: missing or invalid token

**Symptom.** HTTP returns 401 with `WWW-Authenticate: Bearer`, and the CLI exits 77:

```text
Error (unauthorized): Missing or invalid API token.
```

When the CLI has no token at all it also prints:

```text
No API token found. Create one with: roomba-kit token create cli
```

**Cause.** Every route except `GET /v1/health` needs `Authorization: Bearer <token>`. The CLI
and stdio MCP send `ROOMBA_KIT_API_TOKEN` if it's set, else the token in `<state>/cli.token`,
which setup creates. A revoked token, a typo, or a token from another machine's service is
rejected.

**Fix.**

```bash
roomba-kit token list                # names only; tokens are stored hashed
roomba-kit token create my-script    # prints the new token once
roomba-kit token revoke my-script
```

Check that `ROOMBA_KIT_API_TOKEN` isn't set to a stale value in your shell, since it wins over
`cli.token`. A token is shown only when it's created; if you lost it, revoke it and create a new
one.

## `connection_held_elsewhere`

**Symptom.** Commands exit 6 (HTTP 503), and `robots list` shows `held_elsewhere`:

```text
Error (connection_held_elsewhere): Another client holds the robot's local connection. Close the iRobot app or any other integration.
```

During pairing, the password check fails the same way and nothing is saved.

**Cause.** Each robot accepts **one local client at a time**. The kit saw the robot answer on
the network, but its connection was refused because something else holds it: usually the iRobot
app open on a phone, a Home Assistant or other integration, or a second Roomba Kit service.

**Fix.** Close the iRobot app on every phone (force-quit it; backgrounded apps can keep the
connection), and remove or disable other local integrations for that robot. Run exactly one kit
service per robot set. The kit keeps retrying with backoff and reconnects by itself once the
connection is free; watch for it with:

```bash
roomba-kit events watch --type connection.changed
```

## Discovery finds nothing (VPN)

**Symptom.** `roomba-kit discover` or the setup wizard lists no robots, though the robots work in
the app.

**Cause.** Discovery is a UDP broadcast. A VPN that owns the default route sends the broadcast
down the tunnel instead of onto your LAN. Docker Desktop on macOS can't receive it at all.

**Fix.** Broadcast to your subnet's address, which is your router's address with the last
number replaced by 255 (here `192.0.2.255`; yours will differ), or add the robot by its IP from
your router's client list:

```bash
roomba-kit discover --broadcast 192.0.2.255
roomba-kit setup --broadcast 192.0.2.255
roomba-kit robots add 192.0.2.10
```

So the kit's own rediscovery (when a robot's IP changes) uses the same address, set it in
`config.toml`:

```toml
[discovery]
broadcast = "192.0.2.255"
```

See [Setup → Finding robots](setup.md#finding-robots).

## Pairing: `timeout`, `cloud_only` and `password_unavailable`

**Symptom.** `roomba-kit robots password Vacuum` ends with outcome `timeout` or `cloud_only`
instead of `stored`.

**Cause.** The robot releases its local password only for a short window after you hold HOME.
`timeout` means the robot never answered in the retry window (60 s by default): usually HOME
wasn't held, or not long enough. `cloud_only` means it answered at least once but released no
password (internally, `password_unavailable`: "The robot answered but didn't release a
password."), which usually means the robot serves its password only to the iRobot account.

**Fix.**

1. Put the robot on its dock (the Clean Base for an i5+), awake and on the same network.
2. Start `roomba-kit robots password Vacuum`, then hold **HOME** about 2 s until it chimes, and
   release. Use `--timeout-s 120` if you need longer.
3. Still `cloud_only`? Get it from the account: `roomba-kit cloud login`, then
   `roomba-kit cloud password Vacuum`. Or type a password you know with
   `roomba-kit robots password Vacuum --manual`.

`cloud password` can return `unavailable` with a reason: a cloud status reason (see below),
`not_in_account` (the account doesn't list that robot), or `auth_failed` (the robot rejected the
password the account gave). Every password is checked by connecting before it's saved, so a
failed attempt never replaces a working one. A pairing check can also fail with
`connection_held_elsewhere` ([above](#connection_held_elsewhere)).

## Robot `auth_failed`

**Symptom.** `robots list` shows `auth_failed`, and commands exit 7:

```text
Error (auth_failed): The robot rejected its stored password. Re-pair it with `roomba-kit robots password`.
```

**Cause.** The robot no longer accepts the stored password, typically after a factory reset or
Wi-Fi re-setup. The kit stops retrying until the credentials change.

**Fix.** Re-pair: `roomba-kit robots password Vacuum`, or `roomba-kit setup` → *Re-pair a robot*.

## Cloud `auth_failed` and `cloud_unavailable`

The cloud is optional, and none of this affects local control: robot commands, state and events
never wait on it. See [Cloud extras](cloud.md).

**Symptom.** Cloud operations (`floor-plan`, `cloud history`, `coverage`, `cloud password`,
`rooms import`) exit 9 (HTTP 503) with `cloud_unavailable` and a `details.reason`, and
`roomba-kit cloud status` shows `unavailable` with the same reason. `cloud login` reports
`failed` with a reason.

**Cause and fix, by reason:**

| Reason | Means | What to do |
|---|---|---|
| `not_configured` | No account is set up | `roomba-kit cloud login`, or ignore: local control doesn't need it |
| `disabled` | `[cloud] enabled = false` in `config.toml` | Set it to `true` if you want the extras |
| `auth_failed` | iRobot rejected the login: wrong email or password, or the account is locked after too many attempts | Check the password in the iRobot app, then `roomba-kit cloud login` again. The kit doesn't retry rejected credentials, because repeated bad logins extend a lock. A lock clears by itself. |
| `credentials_unreadable` | `credentials.toml` can't be read, usually its mode | See [file permission errors](#file-permission-errors) |
| `connection_failed`, `connection_broken`, `timeout` | The cloud is unreachable or slow | Nothing; the kit retries with backoff (up to 15 min). Check your internet connection or VPN. |
| `ssl_local_trust_store` | This machine can't check iRobot's certificate | A local CA-store problem: waiting won't fix it |
| `ssl_certificate_expired`, `ssl_unverified` | iRobot's certificate is expired or unverifiable | Usually fixed on iRobot's side within hours |
| `not_yet_published` | The data isn't in the cloud yet: coverage for a mission that just ended, or older than the ~30 most recent | Retry in a few minutes |
| `rate_limited`, `request_refused`, `response_malformed` | The cloud refused or mangled one request | Retry later. A persistent `response_malformed` can mean iRobot changed its unofficial API. |

Floor plans and cloud history are served from cache while the cloud is down, so you may see
data with an older `retrieved_at` rather than an error. Each change in cloud status is published
as a `cloud.status` event.

## `unconfirmed` outcomes

**Symptom.** A command returns `outcome: unconfirmed`, HTTP 202, and the CLI exits 3:

```text
robot_id   00000000000000000000000000000001
command    dock
outcome    unconfirmed
state      {"activity":"cleaning","mission":{…}}
```

**Cause.** `unconfirmed` is an outcome, not a failure. The command was sent, but the robot
didn't report the expected state within that command's window (5 s for `find`, 10 s for `pause`,
`resume`, `stop` and `dock`, 15 s for `evac`, 20 s for `clean`, `clean_rooms` and `train`, 180 s for
`reboot`). The robot may still act on it a moment later, or it may have been unable to: stuck,
lid open, bin full, tank empty, battery too low.

**What to check.**

```bash
roomba-kit state Vacuum                  # what it's doing now
roomba-kit events list --robot Vacuum --limit 20
```

- Did the state catch up? A slow robot often reports the change a few seconds late.
- Is there an `error.raised` or `robot.stuck` event, or a bin, tank or lid problem in `state`?
- Is `robots list` still `connected`? A link that dropped right after sending can't confirm.

Don't blindly retry motion commands; read the state first. Settings have a similar outcome,
`not_applied` (exit 3), when the read-back differs: see [Settings](settings.md#when-a-write-isnt-applied).

## `dock_search_timeout`

**Symptom.** An `alert.dock_search_timeout` event, with `data: {mission_id, minutes}`:

```bash
roomba-kit events watch --type alert.dock_search_timeout
```

**Cause.** The robot has been `returning` for longer than `alerts.dock_search_minutes`
(default 5). It fires at most once per mission. The i5 in particular can hunt for its dock for a
long time when it can't see the dock's infrared signal.

**Fix.**

- **Placement.** Put the dock on a hard, level floor against a wall, with clear space on both
  sides and in front (the robot's manual gives the exact clearances), and away from stairs.
- **Line of sight.** Nothing between the robot's usual return path and the dock: furniture
  skirts, cords, a door that swings shut.
- **Clean the sensors.** Wipe the dock's IR window and charging contacts, and the robot's
  sensors and charging contacts, with a dry cloth.
- If the robot is genuinely stuck, there's usually a `robot.stuck` event too. Send it home again
  with `roomba-kit dock Vacuum`, or carry it.

To change the threshold:

```toml
[alerts]
dock_search_minutes = 10
```

## File permission errors

**Symptom.** The service refuses to start (`service start` fails; `service logs` shows the
message), or a running service reports `credentials_unreadable` for the cloud and logs
`credentials file is unreadable (check its permissions)`:

```text
Error (invalid_request): /home/you/.config/roomba-kit/credentials.toml has mode 0644, which lets other users read secrets. Fix it with: chmod 600 /home/you/.config/roomba-kit/credentials.toml
```

**Cause.** `credentials.toml` holds robot passwords, the account login and token hashes. The kit
fails closed: it won't read the file while anyone but its owner can, and keeps refusing until
it's fixed. Copying the file, restoring a backup, or a bind mount in Docker are the usual ways it
ends up too open.

**Fix.**

```bash
roomba-kit config path                      # where the file lives
chmod 600 ~/.config/roomba-kit/credentials.toml
roomba-kit service start
```

In a container, the file must also be owned by the uid the service runs as (1000 by default); a
0600 file owned by someone else is unreadable to it. See
[Deployment → Volumes and file permissions](deployment.md#volumes-and-file-permissions).

## macOS: works in a terminal, not under launchd

**Symptom.** After `roomba-kit service install`, robots stay `offline` and discovery finds
nothing, but `roomba-kit service start` from a terminal works.

**Cause (inferred, not confirmed on hardware).** macOS Local Network privacy. A process that
reaches devices on the LAN needs permission, and a process started by launchd may never get the
prompt a terminal app shows, so its LAN traffic is silently blocked.

**Fix.** Open **System Settings → Privacy & Security → Local Network** and allow the Python the
service runs (the interpreter `roomba-kit service install --print` shows). Then:

```bash
roomba-kit service uninstall
roomba-kit service install
```

## Still stuck

- `roomba-kit config show` prints the effective configuration, secrets masked as `"***"`.
- `roomba-kit capabilities Vacuum` shows what the robot's model supports; `not_supported` (exit 5)
  means the model lacks that capability. See the [support matrix](reference/support-matrix.md).
- `robot_offline` (exit 6) means the robot didn't answer at all: check it's powered, on Wi-Fi and
  on the same network. The kit rediscovers it by ID if its IP changed.
