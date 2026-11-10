# Live position

Some robots will tell you where they are during a mission. The kit asks for you and gives you
the answer two ways: **on request** (`get_position`) and as a **stream** of `position` events
while the robot is cleaning or heading home.

## Which models support it

| Model | `live_position` | Notes |
|---|---|---|
| Braava jet m6 | ✅ verified | Answers about every 2 s while cleaning; answers "no fix" while docked. |
| Roomba i5/i5+ | ❌ not supported | The firmware never answers a position request, locally or through the cloud. Use the [cloud timeline](#room-level-alternative-the-cloud-timeline). |

The kit decides from the model profile, not by asking the robot. Requesting position from a
model without `live_position` fails with `not_supported` straight away, and the robot is never
contacted. See the [support matrix](reference/support-matrix.md) for every capability.

## What a fix looks like

```json
{
  "robot_id": "00000000000000000000000000000002",
  "at": "2026-11-08T20:41:07Z",
  "x_m": 1.82,
  "y_m": -0.47,
  "heading_rad": 3.06,
  "map_id": "<map id>",
  "map_version": "<map version>"
}
```

| Field | Meaning |
|---|---|
| `x_m`, `y_m` | Metres from the dock, the origin of the robot's own map frame. The kit passes the robot's axes through unchanged. |
| `heading_rad` | Heading in radians, between −π and π. **It wraps at π**: a turn can read 3.06 then −2.63. Unwrap it before computing heading deltas, or a 30° turn looks like 330°. |
| `at` | When the robot took the fix. |
| `map_id`, `map_version` | The stored map the coordinates belong to. Fixes from different map versions aren't comparable. |

### "No fix" is not an error

A position-capable robot that doesn't currently know where it is (typically when docked)
answers with:

```json
{"no_fix": true}
```

That's a normal result (HTTP 200, CLI exit 0), and distinct from `not_supported`, which
means the model can never report position.

If a capable robot doesn't answer within 2 s, `get_position` fails with `timeout`.

## On request

**CLI**

```bash
roomba-kit position Mop
# 20:41:07  x=1.82 m  y=-0.47 m  heading=3.06 rad  map=<map id>@<map version>

roomba-kit position Mop          # while docked
# no_fix

roomba-kit position Vacuum       # an i5
# Error (not_supported): Roomba i5/i5+ does not support live_position.   (exit 5)
```

**HTTP**

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8765/v1/robots/Mop/position
```

An i5 gets `422` with
`{"error": {"code": "not_supported", "details": {"profile": "roomba_i5", "capability": "live_position"}, …}}`.

**MCP**

Call the `get_position` tool with `{"robot": "Mop"}`. On an i5 the tool error starts with
`not_supported`.

## The stream

While a capable robot's activity is `cleaning`, `mapping` or `returning`, the service polls it every
2 s and publishes each fix as a `position` event (the payload is the fix above). It stops
polling when the robot pauses, docks or gets stuck, and starts again when it resumes. "No fix"
answers produce no event.

`position` events are **live only**: they aren't written to the event log, so `events list`
and a resumed stream (`since` / `Last-Event-ID`) won't replay them. Set
`events.persist_position = true` in `config.toml` if you want them kept.

**CLI**

```bash
roomba-kit position Mop --watch
# no_fix                                         ← docked: the current answer first
# 20:41:07  x=1.82 m  y=-0.47 m  heading=3.06 rad  map=…
# 20:41:09  x=1.95 m  y=-0.41 m  heading=2.98 rad  map=…
```

`--watch` prints the current answer, then follows the stream until Ctrl-C. Nothing new
prints while the robot isn't cleaning, mapping or returning. With `--json` it prints the
`get_position` body, then one event envelope per line. If the service closes the stream,
the command reconnects with `Last-Event-ID`.

**HTTP (SSE)**

```bash
curl -N -H "Authorization: Bearer $TOKEN" -H "Accept: text/event-stream" \
  "http://127.0.0.1:8765/v1/events?types=position&robot=Mop"
```

Each frame carries `id: <seq>`, `event: position` and the event envelope as `data`. See
[State and events](state-and-events.md).

**MCP**

Use `events_wait` with `types: ["position"]` and `robot`, passing the returned `next` as
`since` on the following call.

### When polling stops for good

A robot that has **never** answered a position request is asked three times (2 s timeout
each) and then left alone for the rest of that mission, so a silent robot doesn't cost
anything. The next mission tries again. Once a robot has answered at least once, timeouts
never stop polling. Polling runs outside the robot's command queue, so it never delays a
command or a settings change.

## Room-level alternative: the cloud timeline

For models without live position (the i5), or when you only need "which room is it in", use
the iRobot cloud's mission timeline. With cloud credentials configured, the service publishes
`cloud.timeline` events within seconds: `start`, `travel` (with the destination room),
`room`, `traversal`, `evac` and `fin`. It's room-level, not coordinates, and it needs the
cloud; see [Cloud extras](cloud.md).

After a mission, the cloud also publishes the full coverage path (`get_coverage`), minutes
after the robot docks.
