# Contributing

Bug reports, model profiles and fixes are welcome. Open an issue first for anything bigger than a
small fix, so we can agree on the shape before you write it.

## Dev setup

```bash
uv sync
uv run pytest -m "not hardware"          # full mocked suite; no robot needed
uv run ruff check && uv run ruff format --check
uv run mypy src
uv run lint-imports                      # layering contracts in .importlinter
uv run python scripts/gen_docs.py        # regenerate docs/reference/ (CI runs --check)
uv run roomba-kit serve                  # run the service in the foreground
```

CI runs all of the above on Python 3.13 and 3.14. Tests run against `FakeTransport`, built from
scrubbed real payloads in `tests/fixtures/captures/`, so you don't need a robot to contribute.

## How the code is laid out

One service owns every robot connection; the CLI, HTTP API and MCP server are its clients.
import-linter enforces the layering:

- `core` imports nothing internal. Handlers are typed against the `OperationContext` protocol in
  `core/registry.py`, so `core` never imports `service`.
- `profiles` → `core`. `transport` → `core`.
- `service` → `core`, `profiles`, `transport`, `config`.
- `api`, `mcp`, `cli` → `core`, `service`, never `transport` directly.
- Only `transport/local.py` imports `roombapy`; only `transport/cloud.py` imports
  `roombapy_prime`. Their types and exceptions never leave those modules.

### Adding an operation

Every capability is one `@operation(...)` in `src/roomba_kit/service/operations/<area>.py`. The
HTTP routes, MCP tools and CLI commands are generated from that registry, so adding an operation
never touches `api/`, `mcp/` or `cli/`. Then:

1. Run `scripts/gen_docs.py` to regenerate the reference docs.
2. Run the contract tests. If the API or MCP surface changed on purpose, refresh the snapshots
   with `UPDATE_SNAPSHOTS=1 uv run pytest tests/contract`, and update
   [`docs/contracts/`](docs/contracts/) to match.

### Adding a robot model

See [Adding a model](docs/guides/adding-a-model.md). A profile is data plus a few small mappings,
and never touches the surfaces.

## Hardware verification

A capability only counts as `verified` after it passes on a real robot:

```bash
roomba-kit verify <robot> <capabilities…> [--allow-motion] --write-report
```

The report lands in `docs/verification/<profile>.md`, and the profile cites it. Before you run it,
dock and charge the robot and close the iRobot app: each robot accepts **one** local connection.
Don't run `train` unless you mean it, because a mapping run can re-create the map and change room
IDs.

## Secrets

Never commit robot passwords, cloud credentials, API tokens, real robot IDs (BLIDs) or real IPs,
whether in code, fixtures, docs, logs, snapshots or commit messages. Fixtures use the robot IDs
`00000000000000000000000000000001` (i5) and `…0002` (m6), with IPs in `192.0.2.0/24`.
`scripts/capture_fixture.py` scrubs new captures; check its output before committing.
