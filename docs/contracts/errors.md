# Contract: Error Catalogue (v1)

These are the only error codes any surface may return. Messages never contain
secrets.

| Code | Meaning | HTTP | CLI exit |
|---|---|---|---|
| `invalid_request` | Bad or missing parameters, empty room list | 400 | 2 |
| `unauthorized` | Missing or invalid token | 401 | 77 |
| `robot_not_found` | No configured robot matches | 404 | 4 |
| `ambiguous_name` | A name matches several robots or rooms; `details.candidates` lists them | 409 | 4 |
| `room_not_found` | A room name or ID isn't on the current map; `details.valid` lists valid rooms | 404 | 4 |
| `not_supported` | The model lacks the capability; `details: {profile, capability}` | 422 | 5 |
| `unsupported_model` | The robot has no profile; the message points to `docs/guides/adding-a-model.md` | 422 | 5 |
| `robot_offline` | The robot is unreachable right now | 503 | 6 |
| `connection_held_elsewhere` | Another client holds the robot's single local connection | 503 | 6 |
| `auth_failed` | Robot or cloud rejected the stored credential | 502 | 7 |
| `unconfirmed` | The command was sent but the state change wasn't observed within the window; `details.state` holds the state | 202 (body is a CommandResult with `outcome=unconfirmed`) | 3 |
| `not_applied` | The setting was written but read back differently; `details.actual` | 409 | 3 |
| `map_changed` | The request references an outdated map version | 409 | 8 |
| `no_map` | The robot has no stored map | 409 | 8 |
| `cloud_unavailable` | Cloud not configured, unreachable, rejected, or data not yet published; `details.reason` | 503 | 9 |
| `password_unavailable` | The robot won't release its password locally | 409 | 10 |
| `timeout` | A wait operation reached its timeout with no result | 408 | 11 |
| `service_unavailable` | CLI or stdio MCP can't reach the service | — | 69 |
| `internal` | Unexpected failure (logged, without secrets) | 500 | 70 |

Note: `unconfirmed` is an *outcome*, not a failure. HTTP returns 202 with a CommandResult;
MCP returns a normal result with `outcome: "unconfirmed"`; the CLI exits 3.
