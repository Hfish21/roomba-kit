# Rooms

Clean "Laundry Room" then "Office", in that order, by name. Room names come from the robot's
stored map in the iRobot cloud if you've added an account, or from names you give locally. The
cloud is never required.

```bash
roomba-kit rooms import Vacuum                    # cloud configured: fetch IDs and names
roomba-kit rooms list Vacuum
roomba-kit clean Vacuum --room "Laundry Room" --room Office
roomba-kit missions wait Vacuum
```

## Which models support it

| Model | `clean_rooms` | `cloud.rooms` (import) |
|---|---|---|
| Roomba i5/i5+ | ✅ verified: went straight to the Laundry Room and cleaned it | ✅ verified |
| Braava jet m6 | ⚪ **unverified**: no room clean has been tried on the m6 | ✅ verified |

Room IDs on the owner's robots, for orientation: the i5 has 1 Living Room, 3 Den, 4 Office,
5 Primary Bathroom, 6 Primary Bedroom, 7 Laundry Room and 8 Guest Bathroom (there is no room 2).
The m6 has 1 Kitchen; rooms 0 and 2–5 have no name in the app. See the
[support matrix](reference/support-matrix.md) for every capability.

## Where rooms come from

`list_rooms` merges three sources into one list for the robot's **current map**:

| Source | How it gets there | `cloud_name` | `local_name` |
|---|---|---|---|
| Imported | `import_rooms`, from the iRobot cloud | the app's name, or null | kept across re-imports of the same map version |
| Named | `name_room`, on any room on the current map | — | your name |
| Learned | The kit saw the robot echo a room clean (FR-032) | null | null until you name it |

Each room has an **effective name** (`name`): its local name, then its cloud name, then the
placeholder `Room <id>`. So the m6's unnamed rooms show as `Room 0`, `Room 2`, …

Names are unique per robot and map version, ignoring case. A name can't be only digits, since
`7` always means room ID 7. If the cloud has two rooms with the same name (say two "Bathroom"s),
both are stored as `Bathroom (<id>)`; asking for plain "Bathroom" fails with `ambiguous_name`
and lists them. Give one a local name to make it unambiguous.

### Importing from the cloud

```bash
roomba-kit rooms import Vacuum     # → {"imported": 7, "map": {"map_id": "…", "version": "…"}}
```

Import replaces what's stored for the robot with the cloud's current map. Local names on that
same map version are kept; rooms stored for older versions are dropped.

The result is cached in the kit's state database, so rooms stay usable while the cloud is down
or logged out (FR-043). `rooms list` shows `retrieved_at`, when they were last fetched. If the
cloud is unreachable, `import_rooms` fails with `cloud_unavailable` and nothing stored changes.

### Without the cloud: learned rooms and local names

The kit watches the robot's echo of its last command. When any client (the iRobot app, a
schedule, the kit) starts a room clean, the room IDs it used are recorded against the robot's
current map version, even with no cloud account. They then appear in `rooms list` as
`Room <id>`:

```bash
roomba-kit rooms list Vacuum               # 3 and 6 learned after an app-started room clean
roomba-kit rooms name Vacuum 3 Den         # PUT /v1/robots/{robot}/rooms/3  {"name": "Den"}
roomba-kit clean Vacuum --room Den
```

To learn a room's ID without the cloud, start a clean of just that room from the iRobot app
once, then check `rooms list` while it runs.

The robot keeps its last echo indefinitely, so the one it reports when the service starts may
predate a map change. That echo is only tied to the current map if it names the current map
version; otherwise its IDs are noted (with when they were seen) but not made usable. Room
cleans seen while the service runs always count.

## Cleaning rooms

`clean_rooms` takes the rooms by name or ID, in cleaning order:

| Surface | Call |
|---|---|
| CLI | `roomba-kit clean ROBOT --room NAME_OR_ID [--room …] [--unordered]` |
| HTTP | `POST /v1/robots/{robot}/commands/clean_rooms` `{"rooms": ["Laundry Room", "Office"], "ordered": true}` |
| MCP | tool `clean_rooms` with the same params |

`roomba-kit clean` is an alias: with no `--room` it's the whole-home `clean`; with rooms it's
`clean_rooms`. The generated command is also at `roomba-kit rooms clean ROBOT --rooms …`, which
additionally takes `--room-options`. Both exit like any other command: `0` when the robot
reported cleaning, `3` when it was sent but not confirmed, `4` for an unknown room, `8` for a
changed map.

- **Ordering.** `ordered` defaults to true: the robot is asked to clean the rooms in the
  order given. `--unordered` (`"ordered": false`) lets it choose. A room listed twice is sent
  once.
- **Never an empty list.** The robot treats an empty room list as "clean everything", so the
  kit refuses one with `invalid_request` before anything is sent. Use `clean` for the whole
  home.
- **Unknown rooms.** A name or ID that isn't on the current map fails with `room_not_found`,
  and `details.valid` lists the rooms you can use. Nothing is sent.
- **Per-room passes** (`room_options`, ⚪ unverified on both robots): `{"Office": {"passes":
  "two"}}`, keyed by an entry of `rooms`. `passes` is `auto`, `one` or `two`. On the CLI, pass
  it as JSON: `roomba-kit rooms clean Vacuum --rooms Office --room-options '{"Office":
  {"passes": "two"}}'`.

The command is confirmed when the robot reports cleaning. Follow the run with `missions wait`;
its mission record lists `rooms_requested`.

## When the map changes

Room IDs belong to one version of the robot's stored map. A new mapping run (`train`, or
re-creating the map in the app) can renumber rooms, so a name from the old map could send the
robot to the wrong room. The kit compares stored rooms with the map version the robot reports
now and refuses a mismatch with **`map_changed`** (HTTP 409, CLI exit 8), sending nothing:

```json
{"error": {"code": "map_changed", "message": "Room 'Laundry Room' belongs to map version …",
  "details": {"current": {"map_id": "…", "version": "…"},
              "requested": {"map_id": "…", "version": "…"}}}}
```

`rooms list` shows `"stale": true` while any stored room is for an older map. To recover:

1. With the cloud: `roomba-kit rooms import ROBOT`, after the app shows the new map.
2. Without it: let the kit learn the new IDs (a room clean from the app), then
   `rooms name` them again. Local names apply to one map version and aren't carried over,
   because the IDs may now mean different rooms.

A robot with no stored map fails room requests with **`no_map`** (exit 8). Map it first.

Learned IDs are tied to the map version they were seen on, so they go stale with it too.

> **Note.** The m6's current map version was created by the robot itself (the cloud's map
> record says so), so it may move to a new version without a mapping run you started. If it
> does, its stored rooms go stale and room requests fail with `map_changed` until you
> re-import or rename. That keeps the stale-ID guarantee, at the cost of re-importing more
> often on the m6. This is inferred from the cloud record, not observed.

## Unverified details

- **m6 room cleaning.** Not tried on hardware. The kit sends the same command shape as on
  the i5.
- **Multi-room order.** Only a single-room clean has run on hardware (the i5's Laundry Room).
  The verified clean sent `ordered: 1`; that the robot follows the order across several rooms
  (or ignores it with `ordered: 0`) hasn't been observed.
- **Which map version the m6 expects.** A room clean names the map by ID and a "user" map
  version. On the i5 that equals the version the robot reports. On the m6 the cloud lists a
  different user version, and the kit sends it when rooms were imported. Without an import,
  the kit uses the version from the robot's last room-clean echo, then the version the robot
  reports.
- **Per-room passes** on either robot.
