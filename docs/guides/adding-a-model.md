# Adding a model

A robot model is supported when an installed **model profile** matches its SKU. A profile is
one Python module exporting `PROFILE: ModelProfile`: data and a few small mappings, no
surface code. The CLI, HTTP API and MCP server are generated from the operation registry and
read capabilities from whatever profiles are installed, so adding a model never touches
`api/`, `mcp/` or `cli/`. `tests/profiles/test_fake_model.py` enforces that.

The convention is specified in [the model-profile contract](../contracts/model-profile.md). This
guide walks through its five steps, using the Braava jet m6 profile
(`src/roomba_kit/profiles/braava_m6.py`) as the worked example, as if V1 hadn't shipped it.

## Before you start

- The robot is on your Wi-Fi and added to the kit: `roomba-kit discover`, then
  `roomba-kit robots add ADDR` and `roomba-kit robots password ROBOT`. Until a profile
  matches, it's listed with `supported: false`, and every command returns
  `unsupported_model` without contacting the robot.
- Note its SKU from `roomba-kit discover` or `roomba-kit robots show ROBOT`. The m6 reports
  `m611020`.
- Pick a profile ID. It's `snake_case`, it's permanent (it appears in docs, discovery output
  and verification reports), and it names the model family, not one robot: `braava_m6`.

## 1. Capture the robot's raw state

```bash
roomba-kit service stop        # robots accept one local connection
uv run python scripts/capture_fixture.py ROBOT --profile braava_m6
roomba-kit service start
```

`scripts/capture_fixture.py` is a developer tool, not a kit surface: raw protocol payloads
never appear on the API, MCP or CLI. It:

- refuses to run while the service's pidfile is live, or while a service answers at the
  configured `server.host`/`server.port`. A service on another host can't be detected, so
  stop that one yourself;
- reads the robot's address and password from the kit's config and credentials (`ROBOT` is
  its ID or name);
- connects directly over the local MQTT connection and merges 30 s of reported state
  (`--seconds N` to change it). Dock the robot first, so the capture is a stable docked
  state;
- scrubs the payload and writes `tests/fixtures/profiles/<id>/state.json` (`--out PATH` to
  write elsewhere).

Scrubbing follows `tests/fixtures/captures/README.md`:

- credential and network keys are removed at any depth: `passwordHash`, `netinfo`, `wlcfg`,
  `mac`, `svcEndpoints`, `country`, `tz` and the rest of that list, plus any key containing
  `password`, `ssid`, `serial`, `token`, `email` or `username`;
- the robot's BLID becomes `00000000000000000000000000000001` (`--placeholder-id` to
  change it), and other 32-hex IDs become `FFFF…000N`;
- IPs become `192.0.2.N`, MACs `00:00:5e:00:53:NN` and emails `user@example.com`;
- the display name becomes `Robot` (`--name` to change it).

**Read the file before committing it.** Firmware can add keys that the scrubber doesn't know
about. If you find an identifier, add its key to `REMOVED_KEYS` in the script, with a canary
case in `tests/profiles/test_capture_scrub.py`, and capture again.

## 2. Write the profile

Copy the closest existing profile: a mop starts from `braava_m6.py`, a vacuum from
`roomba_i5.py`. Then set its identity, SKU patterns and capabilities. **Every capability
starts as `unverified()`**, however sure you are:

```python
"""Braava jet m6 (SKU `m6…`, firmware `sanmarino`)."""

from roomba_kit.core.capabilities import Capability as C
from roomba_kit.core.capabilities import Verification
from roomba_kit.profiles.base import ModelProfile

_UNVERIFIED = Verification.unverified()

PROFILE = ModelProfile(
    id="braava_m6",
    display_name="Braava jet m6",
    sku_patterns=[r"^m6"],
    capabilities=dict.fromkeys(
        (
            C.CLEAN,
            C.PAUSE,
            C.RESUME,
            C.STOP,
            C.DOCK,
            C.FIND,
            C.REBOOT,
            C.TRAIN,
            C.CLEAN_ROOMS,
            C.STATE_PAD,
            C.STATE_TANK,
            C.STATE_LID,
            C.LIVE_POSITION,
            C.SETTING_PASSES,
            C.SETTING_EDGE_CLEAN,
            C.SETTING_CHILD_LOCK,
            C.SETTING_ECO_CHARGE,
            C.SETTING_SCHEDULE_HOLD,
            C.SETTING_PAD_WETNESS,
            C.SETTING_SPRAY_OVERLAP,
            C.CLOUD_ROOMS,
            C.CLOUD_HISTORY,
            C.CLOUD_COVERAGE,
            C.CLOUD_TIMELINE,
        ),
        _UNVERIFIED,
    ),
)
```

What to decide, field by field:

| Field | For the m6 |
|---|---|
| `sku_patterns` | Regexes searched in the discovery SKU. `^m6` covers the jet m6 family (`m611020`, …). Keep them narrow: the first matching profile by ID wins. |
| `capabilities` | Only what the model has. The m6 has a pad, a tank and a lid, so it declares `state.pad`, `state.tank` and `state.lid`, and leaves out `state.bin`, `state.clean_base` and `evac`. Anything not listed is `not_supported`, and the kit never contacts the robot for it. |
| `settings` | `{name: SettingMapping}` for each writable setting: `read`, `write`, the `group` of protocol keys written together, and `confirm`. Each setting needs its `setting.<name>` capability declared, or the profile fails to load. |
| `command_overrides` | A `CommandSpec(predicate, window_s)` when the model confirms a command differently from the shared defaults in `service/confirm.py`. |
| `protocol_commands` | A kit command → firmware command rename, where the model differs. |
| `error_texts`, `not_ready_texts` | Readable text for this model's error and not-ready codes. |

Rules (contracts/model-profile.md):

- A profile imports only `roomba_kit.core` and `roomba_kit.profiles`, never a surface module.
  `uv run lint-imports` checks it.
- Protocol keys (`detectedPad`, `tankLvl`, …) live only in profiles and `service/normalize.py`.
  Kit models and surfaces never expose them.
- Capabilities come from the closed `Capability` enum in `core/capabilities.py`. A model that
  needs a capability the kit doesn't have yet is a core change, with its own operation, not a
  profile change.

Register the profile under the `roomba_kit.profiles` entry-point group. In-tree, that's
`pyproject.toml`:

```toml
[project.entry-points."roomba_kit.profiles"]
braava_m6 = "roomba_kit.profiles.braava_m6:PROFILE"
```

Then run `uv sync` so the entry point is installed. A third-party package registers the same
way in its own `pyproject.toml`, and the kit loads it without any change here.

## 3. Add the fixture and run the profile tests

Commit the captured `tests/fixtures/profiles/braava_m6/state.json`, then add the profile's
expectations to `EXPECTED` in `tests/profiles/test_profiles.py`: the exact capability set with
each status, and what the fixture normalizes to (activity, battery, pad, tank, lid, counters,
…).

```bash
uv run pytest tests/profiles
```

The tests find every installed profile through the entry points, so a new profile without a
fixture or without expectations fails. They check that:

- the fixture's own SKU matches the profile;
- the declared capabilities and verification statuses are exactly the expected ones;
- replaying the fixture through `service.normalize.normalize` gives the expected state;
- telemetry the profile doesn't declare stays empty (for the m6, `bin` and `clean_base` are
  always `null`).

If the normalized state looks wrong (an `unknown` activity on a docked robot, say), the model
reports something `normalize.py` doesn't map yet. Fix it there, with a test in
`tests/unit/test_normalize.py`.

## 4. Verify on hardware

A capability is `verified` only after it has been exercised on a real robot of that model. Until then, discovery, `roomba-kit capabilities ROBOT`, the tool
descriptions and the support matrix all show it as `unverified`.

```bash
roomba-kit verify ROBOT                               # non-motion checks only
roomba-kit verify ROBOT --allow-motion --write-report  # prompts before moving the robot
```

Run it with the robot docked and charged, and with nothing else holding its connection (no
iRobot app). Never run `train` without the owner's OK: a mapping run can change room IDs.

`--write-report` appends the run to `docs/verification/braava_m6.md`. Commit the report, then
flip each passing capability to `verified`, citing the report's anchor:

```python
_VERIFIED = Verification.verified(run="docs/verification/braava_m6.md#2026-11-08")

PROFILE = ModelProfile(
    ...,
    capabilities={
        **dict.fromkeys((C.CLEAN, C.PAUSE, C.RESUME, C.DOCK, C.FIND, ...), _VERIFIED),
        **dict.fromkeys((C.STOP, C.REBOOT, C.TRAIN, ...), _UNVERIFIED),
    },
)
```

`Verification.verified()` refuses an empty `run`, so a capability can't be marked verified
without citing evidence. Leave a capability `unverified` if its check failed or was skipped,
and say why in the report. Update `EXPECTED` in `tests/profiles/test_profiles.py` to match.

## 5. Regenerate the reference docs

```bash
uv run python scripts/gen_docs.py
```

`docs/reference/support-matrix.md` gains a `braava_m6` column (✅ verified, ⚪ unverified,
— not supported) and lists every verification run. `docs/reference/operations.md` adds the
profile to each operation's "Supported on" line. CI runs `gen_docs.py --check`, so stale docs
fail the build.

## What you don't change

Nothing under `src/roomba_kit/api/`, `src/roomba_kit/mcp/` or `src/roomba_kit/cli/`. Each
surface builds its descriptions from `service/describe.py`, which reads the installed
profiles, so the new model appears everywhere at once:

- **MCP:** each tool's description lists it under "Supported on" and "Verification".
- **HTTP:** the OpenAPI description of each operation lists it the same way.
- **CLI:** each command's `--help` (e.g. `roomba-kit position --help`) shows the same lines.

Discovery reports a matching robot with `supported: true` and the profile ID, and unsupported
capabilities fail with `not_supported` naming the model and capability.

A finished change touches only the profile module, its `pyproject.toml` entry, its fixture,
`tests/profiles/test_profiles.py`, the verification report and the regenerated reference
docs. `git diff --stat` should show nothing else.
