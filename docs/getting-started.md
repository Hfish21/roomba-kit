# Getting started

This guide takes you from a robot on your Wi-Fi to a confirmed `clean` command in about 15
minutes. You don't need the iRobot app or an iRobot account.

## Before you start

- A Roomba i5/i5+ or Braava jet m6 that's already on your home Wi-Fi. If it isn't, set it up with
  the iRobot app once. After that, Roomba Kit doesn't need the app.
- A Mac or Linux machine on the same network, with Python 3.13+ and
  [uv](https://docs.astral.sh/uv/) installed.
- **Close the iRobot app on every phone.** Each robot accepts only one local connection, and the
  app competes with the kit for it.
- The robot on its dock, charged.

## 1. Install

```bash
git clone git@github.com:Hfish21/roomba-kit.git
cd roomba-kit
uv sync
```

Every command below runs as `uv run roomba-kit …` from this directory. To drop the `uv run`, install
the CLI with `uv tool install .`.

## 2. Run setup

```bash
uv run roomba-kit setup
```

The wizard does five things. It explains each one as it goes:

1. **Creates an API token for this machine.** The CLI uses it to talk to the service.
2. **Starts the service** if it isn't running (say yes).
3. **Finds your robots.** For each one, it shows the name, model, SKU, firmware, address and robot
    ID, then asks whether to add it.

    !!! tip "Nothing found?"
        A VPN can own the network route, and discovery then finds nothing. The wizard asks for
        a broadcast address or the robot's IP. Type your subnet's broadcast address, which is your
        router's address with the last number replaced by 255 (e.g. `192.0.2.255`). Or type the
        robot's IP from your router's client list. You can also run
        `roomba-kit setup --broadcast 192.0.2.255`.

4. **Pairs each robot.** This gets the robot's local password from the robot itself:
    1. Place the robot on its dock, then press Enter.
    2. Press and hold **HOME** for about 2 seconds, until it chimes.
    3. Release. The kit keeps retrying for 60 seconds.

    The password is checked by connecting with it, then stored. It's never shown. If the robot won't
    release it, choose **retry** (hold HOME until you hear the chime), **manual** (type it if you
    have it), or **skip** and get it through the iRobot account in the next step.
5. **Offers iRobot account credentials, which are optional.** Answer **no** to stay fully local.
    See [Setup → Cloud credentials](setup.md#cloud-credentials) for what an account adds.

## 3. Check the robot

```bash
uv run roomba-kit robots list
```

```text
id                                name    profile_id  supported  sku      firmware  address     connection  …
00000000000000000000000000000001  Vacuum  roomba_i5   yes        i5G5020  …         192.0.2.10  connected   …
```

Wait for `connection` to read `connected`. This takes a few seconds after pairing. Use the robot's
name or its ID wherever a command takes `ROBOT`.

## 4. Clean

```bash
uv run roomba-kit clean Vacuum
```

```text
robot_id   00000000000000000000000000000001
command    clean
outcome    confirmed
state      {"activity":"cleaning","mission":{…}}
waited_ms  2140
```

The robot leaves its dock. `outcome: confirmed` means the kit saw the state change on the robot. It
doesn't just mean the command was sent. When the robot doesn't report the change in time, the
result is `unconfirmed` with the current state, and the CLI exits with code 3.

To stop the robot and send it home:

```bash
uv run roomba-kit pause Vacuum
uv run roomba-kit dock Vacuum
```

The other commands are `resume`, `stop`, `find` (plays a sound), `evac` (empties the bin into the
Clean Base, on the i5+ only) and `reboot`. Avoid `train` for now: a mapping run can re-create the
map and change room IDs.

## The same thing over HTTP and MCP

The CLI is one of three surfaces. The HTTP API and the MCP server take the same operations,
parameters and error codes.

**HTTP.** The API listens on `http://127.0.0.1:8765/v1`, and every route except `/v1/health` needs a
token:

```bash
uv run roomba-kit token create my-script          # prints the token once; keep it
export TOKEN=rk_…
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/robots
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8765/v1/robots/Vacuum/commands/clean
```

The OpenAPI docs are at <http://127.0.0.1:8765/v1/docs>.

**MCP.** Point an MCP client at `http://127.0.0.1:8765/mcp` (streamable HTTP) with the header
`Authorization: Bearer <token>`. The tools are named after the operations: `list_robots`, `clean`,
`dock` and so on.

## Day to day

| To | Run |
|---|---|
| Start, stop or check the service | `roomba-kit service start` · `service stop` · `service status` |
| Start it at boot | `roomba-kit service install` (see [Deployment](deployment.md)) |
| Add, re-pair or remove robots, or change the account | `roomba-kit setup` again |
| Check that the kit is up | `roomba-kit health` |
| See the effective config, with secrets masked | `roomba-kit config show` |

If a command prints `Service not running — start it with: roomba-kit service start`, do that.
Every robot command goes through the service, because the service owns each robot's single
connection.

## Next

- [Setup](setup.md): password steps per model, cloud credentials, adding by IP, removing robots.
- [State and events](state-and-events.md): what the robot is doing, live.
- [Rooms](rooms.md): clean specific rooms by name.
- [CLI reference](reference/cli.md) and [operations reference](reference/operations.md).
