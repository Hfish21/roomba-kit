# Roomba Kit

Roomba Kit is a local-first Python kit for controlling a Roomba i5/i5+ and a Braava jet m6 on your
own network, without the iRobot app. One long-running service owns each robot's single local
connection, and every capability is exposed identically through a CLI (`roomba-kit`), an
authenticated HTTP API and an MCP server for LLM agents. The iRobot cloud is optional: it adds room
names, floor plans, mission history, coverage and a live room-level timeline, and nothing local
depends on it.

## Features

- **Control:** clean, clean specific rooms in order, pause, resume, stop, dock, find, empty the
  bin (i5+), reboot. Every command is confirmed against the robot's reported state, not just sent.
- **State and events:** battery, activity, mission, bin, pad and tank state, and a live event
  stream (SSE, CLI `events watch`, MCP) with replay by sequence number.
- **Mission history** recorded locally, with outcomes, plus a dock-search alert.
- **Settings** read and written with read-back (passes, edge clean, child lock, pad wetness, …).
- **Live position** on the Braava jet m6.
- **Guided setup:** discovery, button-press pairing, adding robots by IP.
- **Optional cloud extras:** room import, floor plans, cloud history, coverage maps, live
  room-level timeline, password retrieval.
- **Runs anywhere always-on:** `service install` for launchd or systemd, or Docker Compose.

What each model supports, and what's been verified on real hardware, is in the
[support matrix](docs/reference/support-matrix.md).

## Quickstart

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/), on the same network as the robot.
Close the iRobot app first: each robot accepts one local connection.

```bash
git clone git@github.com:Hfish21/roomba-kit.git && cd roomba-kit
uv sync
uv run roomba-kit setup          # token, service, discovery, pairing, optional cloud
uv run roomba-kit robots list    # wait for connection: connected
uv run roomba-kit clean Vacuum   # outcome: confirmed
```

## Documentation

- [Getting started](docs/getting-started.md): from a robot on your Wi-Fi to a confirmed command.
- [Setup](docs/setup.md), [Deployment](docs/deployment.md),
  [Troubleshooting](docs/troubleshooting.md).
- Reference: [operations](docs/reference/operations.md), [CLI](docs/reference/cli.md),
  [errors](docs/reference/errors.md), [events](docs/reference/events.md).

Build the docs site locally with `uv run mkdocs serve`.
