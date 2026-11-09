# Roomba Kit

Roomba Kit is a local-first Python kit for controlling a Roomba i5/i5+ and a Braava jet m6 on
your own network, without the iRobot app. A single long-running service owns each robot's one
local connection, confirms every command against the robot's reported state, records missions
and events, and exposes all of it through three surfaces with the same operations, parameters
and error codes.

The iRobot cloud is optional. An account adds room names, floor plans, mission history,
coverage maps and a live room-level timeline; nothing local depends on it.

## Three surfaces, one set of operations

| Surface | For | Start here |
|---|---|---|
| **CLI**, `roomba-kit` | You, at a terminal, and shell scripts | [CLI reference](reference/cli.md) |
| **HTTP API**, `http://127.0.0.1:8765/v1` | Scripts, dashboards, other services. Bearer-token auth; OpenAPI docs at `/v1/docs` | [Operations reference](reference/operations.md) |
| **MCP server**, `http://127.0.0.1:8765/mcp` | LLM agents. Tools are named after the operations | [Building consumers](guides/building-consumers.md) |

```bash
roomba-kit clean Vacuum
curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8765/v1/robots/Vacuum/commands/clean
```

Over MCP, call the `clean` tool with `{"robot": "Vacuum"}`. All three return the same
`CommandResult`, with `outcome: confirmed` once the robot reports it's cleaning.

## Where to go

- **New here?** [Getting started](getting-started.md) takes you from a robot on your Wi-Fi to a
  confirmed `clean` in about 15 minutes. [Setup](setup.md) covers pairing, adding robots by IP
  and the optional cloud account.
- **Using it:** [State and events](state-and-events.md), [Rooms](rooms.md),
  [Settings](settings.md), [Live position](position.md), [Mission history](history.md),
  [Cloud extras](cloud.md).
- **Running it all the time:** [Deployment](deployment.md) (launchd, systemd, Docker).
- **Building on it:** [Building consumers](guides/building-consumers.md),
  [Adding a model](guides/adding-a-model.md).
- **Something wrong?** [Troubleshooting](troubleshooting.md).
- **Reference:** [operations](reference/operations.md), [CLI](reference/cli.md),
  [errors](reference/errors.md), [events](reference/events.md), and the
  [support matrix](reference/support-matrix.md) of what each model supports and what's been
  verified on real hardware.
