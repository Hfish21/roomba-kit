# Roomba Kit: agent guide

Start with `HANDOFF.md`. The source of truth is `.specify/memory/constitution.md` and
`specs/001-roomba-kit-v1/` (spec, plan, contracts, tasks). Non-obvious implementation calls are
logged in `specs/001-roomba-kit-v1/decisions.md`.

## Commands

```bash
uv run pytest -m "not hardware"          # full mocked suite; no robot needed
uv run ruff check && uv run ruff format --check
uv run mypy src
uv run lint-imports                      # layering contracts in .importlinter
uv run python scripts/gen_docs.py        # regenerate docs/reference/ (CI runs --check)
uv run roomba-kit serve                  # run the service in the foreground
```

## Layering (enforced by import-linter)

- `core` imports nothing internal. Handlers are typed against the `OperationContext` protocol in
  `core/registry.py`, so `core` never imports `service`.
- `profiles` → `core`. `transport` → `core`.
- `service` → `core`, `profiles`, `transport`, `config`.
- `api`, `mcp`, `cli` → `core`, `service`, never `transport` directly.
- Only `transport/local.py` imports `roombapy`; only `transport/cloud.py` imports
  `roombapy_prime`. Their types and exceptions never leave those modules (Constitution V).

## The operation registry

Every capability is one `@operation(...)` in `src/roomba_kit/service/operations/<area>.py`.
The HTTP routes, MCP tools and CLI commands are generated from the registry, so adding an
operation never touches `api/`, `mcp/` or `cli/`. `load_operations()` imports every module in
that package, so a new module self-registers.

**New files per story.** Story work adds new modules (operations, EventBus subscribers, CLI
extras via `register(group)`) rather than editing shared ones. Derived behaviour subscribes to
the EventBus and never edits `service/session.py`.

After adding or changing an operation, run `scripts/gen_docs.py` and the contract tests.

## Secrets

Never write robot passwords, cloud credentials, API tokens, real BLIDs or real IPs into code,
fixtures, docs, logs, snapshots or commit messages. Fixtures use robot IDs
`00000000000000000000000000000001` (i5) / `...0002` (m6) and IPs in `192.0.2.0/24`.

## Hardware and the verify workflow

CI runs only against `FakeTransport`. A capability is `verified` only after
`roomba-kit verify <robot> <caps…> [--allow-motion] --write-report` passes on a real robot; the
report is committed under `docs/verification/<profile>.md` and the profile cites it. Each robot
accepts one local connection: never run two clients against the same robot. Never run `train`
without the owner's explicit OK.

## SpecKit

`SPECIFY_INIT_DIR=~/context` is set globally. `.specify/scripts/python/common.py` is patched so
the nearest `.specify/` wins. Re-apply that patch after `specify init --force`.
