# Roomba Kit

Roomba Kit is a local-first Python kit for controlling a Roomba i5/i5+ and a Braava jet m6 on your
own network, without the iRobot app. One long-running service owns each robot's single local
connection, and every capability is exposed identically through a CLI (`roomba-kit`), an
authenticated HTTP API and an MCP server for LLM agents. The iRobot cloud is optional: it adds room
names, floor plans, mission history, coverage and a live room-level timeline, and nothing local
depends on it.

## Install

```bash
git clone git@github.com:Hfish21/roomba-kit.git && cd roomba-kit
uv sync
uv run roomba-kit setup
```

Start with the [getting-started guide](docs/getting-started.md).
