# Contract: Model Profiles (the add-a-model convention)

A model profile is a Python module that exports `PROFILE: ModelProfile`. It is registered under
the entry-point group `roomba_kit.profiles`, so in-tree and third-party profiles load the same
way. V1 ships `roomba_i5` and `braava_m6`.

## A profile declares

| Field | Purpose |
|---|---|
| `id`, `display_name` | Identity |
| `sku_patterns` | Regexes matched against the discovery `sku` (e.g. `^i5` for the i5/i5+ family, `^m6` for the jet m6) |
| `capabilities` | `{Capability: Verification}`, where `Verification` is `verified(run="<ref>")` or `unverified()` |
| `settings` | `{SettingName: SettingMapping}` |
| `command_overrides` | Per-command confirmation predicate or window overrides |
| `command_windows` | Per-command confirmation windows in seconds, keeping the default predicate (e.g. the m6's 30 s for pause/resume/stop/dock) |
| `protocol_commands` | Kit command → firmware command where they differ (defaults: `clean`/`clean_rooms` → `start`, `reboot` → `reset`) |
| `dock_needs_pause` | The firmware ignores `dock` while cleaning, so the kit pauses first (true for both V1 models) |
| `error_texts`, `not_ready_texts` | Readable text for codes, overriding shared defaults |
| `pairing_steps` | Optional. What to do at the robot during local password retrieval: returned by `retrieve_password` and printed by `setup` and `robots password`. The first step is done before retrieval starts. Defaults to `DEFAULT_PAIRING_STEPS` in `profiles/base.py` (dock it, hold HOME ~2 s until it chimes, release), which unsupported models also get |

A **SettingMapping** has these fields:

| Field | Purpose |
|---|---|
| `read` | `(reported_state) -> value` |
| `write` | `(value) -> dict` of protocol keys to publish |
| `group` | Keys that must be written together, e.g. `noAutoPasses` + `twoPass` for `passes` |
| `confirm` | `(reported_state, value) -> bool` |

## Rules

1. Profiles contain **no surface code** and do not import surface modules (import-linter).
2. Every declared capability starts as `unverified()` until a `roomba-kit verify` run passes on
   real hardware. The `run` reference points at `docs/verification/<profile>.md#<anchor>`.
3. Capabilities not listed are `not_supported`. The kit never contacts the robot for them.
4. Protocol keys live only in profiles and transport adapters, never in core models or
   surfaces.
5. A new profile must ship a fixture of a scrubbed full state payload from that model, under
   `tests/fixtures/profiles/<id>/state.json`. The profile's tests replay it.

## Adding a model (developer workflow; full guide in `docs/guides/adding-a-model.md`)

1. Capture the robot's raw state with the developer script `scripts/capture_fixture.py`. This is
   a dev tool, not a kit surface: raw payloads never appear on the API, MCP or CLI. It requires the service to be stopped, connects directly, and writes a scrubbed
   `state.json`.
2. Copy the closest profile, then set `sku_patterns` and the capabilities, all `unverified()`.
3. Add the fixture and run `pytest tests/profiles`.
4. Run `roomba-kit verify ROBOT --allow-motion`, commit the report, and flip the passing
   capabilities to `verified`.
5. Run `scripts/gen_docs.py`. The support matrix updates automatically.
