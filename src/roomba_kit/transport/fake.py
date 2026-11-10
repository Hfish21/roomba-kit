"""In-memory transports for tests: a scripted robot, a canned cloud and a password retriever.

Reactions follow the protocol shapes seen on hardware (docs/research/discovery-2026-11-08.md §2).
Every delay is an `asyncio.sleep`, so tests run fast with small values.
"""

import asyncio
import copy
import json
import time
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import PositionFix
from roomba_kit.transport.base import ConnectionCallback, ReportedCallback

FIXTURE_I5_ID = "00000000000000000000000000000001"
FIXTURE_M6_ID = "00000000000000000000000000000002"
_FIXTURE_PREFIX = {FIXTURE_I5_ID: "i5", FIXTURE_M6_ID: "m6"}


def _load(path: Path) -> Any:
    return json.loads(path.read_text()) if path.exists() else None


class FakeTransport:
    """A `RobotTransport` over an in-memory `state.reported` dict, plus test controls."""

    def __init__(
        self,
        robot_id: str,
        reported: dict[str, Any] | None = None,
        *,
        address: str = "192.0.2.10",
        password: str = "",
        reaction_delay: float = 0.0,
        dock_delay: float | None = None,
        evac_duration: float | None = None,
    ) -> None:
        self.robot_id = robot_id
        self.address = address
        self.password = password
        self.reported: dict[str, Any] = copy.deepcopy(reported or {})
        self.reaction_delay = reaction_delay
        self.dock_delay = reaction_delay if dock_delay is None else dock_delay
        self.evac_duration = reaction_delay if evac_duration is None else evac_duration
        self.connected = False
        self.connect_calls = 0
        self.sent_commands: list[tuple[str, dict[str, Any] | None]] = []
        self.published_settings: list[dict[str, Any]] = []
        self.dropped_commands: set[str] = set()
        """Commands accepted on the wire but never echoed or acted on."""
        self.drop_all_commands = False
        self.ignored_settings: set[str] = set()
        """Setting keys published but never reflected back as reported."""
        self.position_requests = 0
        self.positions_never_answer = False
        self._connect_failures: list[KitError] = []
        self._offline: KitError | None = None
        self._positions: list[PositionFix | None] = []
        self._reported_cbs: list[ReportedCallback] = []
        self._connection_cbs: list[ConnectionCallback] = []
        self._tasks: set[asyncio.Task[None]] = set()

    def __repr__(self) -> str:
        return f"FakeTransport({self.robot_id!r}, connected={self.connected})"

    # -- RobotTransport --------------------------------------------------------------------------

    async def connect(self) -> None:
        self.connect_calls += 1
        if self._connect_failures:
            raise self._connect_failures.pop(0)
        if self._offline is not None:
            raise self._offline
        if self.connected:
            return
        self.connected = True
        for cb in list(self._connection_cbs):
            cb(True, None)
        self.emit(copy.deepcopy(self.reported))

    async def disconnect(self) -> None:
        self._cancel_reactions()
        self.connected = False

    def on_reported(self, callback: ReportedCallback) -> None:
        self._reported_cbs.append(callback)

    def on_connection(self, callback: ConnectionCallback) -> None:
        self._connection_cbs.append(callback)

    async def send_command(self, name: str, params: dict[str, Any] | None = None) -> None:
        self._require_connected()
        self.sent_commands.append((name, params))
        if self.drop_all_commands or name in self.dropped_commands:
            return
        self._spawn(self._react(name, params))

    async def publish_settings(self, values: dict[str, Any]) -> None:
        self._require_connected()
        self.published_settings.append(copy.deepcopy(values))
        applied = {k: v for k, v in values.items() if k not in self.ignored_settings}
        if applied:
            self._spawn(self._later(self.reaction_delay, copy.deepcopy(applied)))

    async def request_position(self, timeout: float) -> PositionFix | None:
        self._require_connected()
        self.position_requests += 1
        if self.positions_never_answer:
            await asyncio.sleep(timeout)
            raise TimeoutError
        return self._positions.pop(0) if self._positions else None

    # -- test controls ---------------------------------------------------------------------------

    def emit(self, fragment: dict[str, Any]) -> None:
        """Deliver a `state.reported` fragment as if the robot had published it."""
        self.reported.update(copy.deepcopy(fragment))
        for cb in list(self._reported_cbs):
            cb(copy.deepcopy(fragment))

    def fail_next_connect(self, *codes: ErrorCode) -> None:
        """Make the next `len(codes)` connects raise these codes, in order."""
        self._connect_failures += [KitError(c, f"fake connect failure: {c.value}") for c in codes]

    def drop(self, reason: str = "robot_offline", *, offline: bool = True) -> None:
        """Lose the link. With `offline`, connects keep failing until `restore()`."""
        if offline:
            self._offline = KitError(ErrorCode(reason), f"fake robot unreachable: {reason}")
        self._cancel_reactions()
        if self.connected:
            self.connected = False
            for cb in list(self._connection_cbs):
                cb(False, reason)

    def restore(self) -> None:
        """Make the robot reachable again; the next `connect()` succeeds."""
        self._offline = None

    def script_positions(self, *fixes: PositionFix | None) -> None:
        """Answers for the next position requests; `None` is "no fix". Empty script → no fix."""
        self._positions += fixes

    async def settle(self) -> None:
        """Wait for every pending command reaction and settings echo."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- internals -------------------------------------------------------------------------------

    def _require_connected(self) -> None:
        if not self.connected:
            raise KitError(ErrorCode.ROBOT_OFFLINE, "fake robot not connected", self.robot_id)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _cancel_reactions(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    async def _later(self, delay: float, fragment: dict[str, Any]) -> None:
        await asyncio.sleep(delay)
        self.emit(fragment)

    def _status(self, **changes: Any) -> dict[str, Any]:
        status = dict(
            self.reported.get("cleanMissionStatus") or {"cycle": "none", "phase": "charge"}
        )
        status.update(changes)
        return {"cleanMissionStatus": status}

    async def _react(self, name: str, params: dict[str, Any] | None) -> None:
        await asyncio.sleep(self.reaction_delay)
        now = int(time.time())
        self.emit(
            {
                "lastCommand": {
                    "command": name,
                    "time": now,
                    "initiator": "localApp",
                    **(params or {}),
                }
            }
        )
        status = self.reported.get("cleanMissionStatus") or {}
        if name == "start":
            fresh = status.get("cycle", "none") == "none"
            mission = {"nMssn": status.get("nMssn", 0) + 1, "mssnStrtTm": now} if fresh else {}
            self.emit(self._status(cycle="clean", phase="run", initiator="localApp", **mission))
        elif name == "pause":
            self.emit(self._status(phase="stop"))
        elif name == "resume":
            self.emit(self._status(phase="run"))
        elif name == "stop":
            self.emit(self._status(cycle="none", phase="stop"))
        elif name == "dock":
            self.emit(self._status(phase="hmUsrDock"))
            await asyncio.sleep(self.dock_delay)
            self.emit(self._status(cycle="none", phase="charge"))
        elif name == "evac":
            previous = status.get("cycle", "none")
            self.emit(self._status(cycle="evac"))
            await asyncio.sleep(self.evac_duration)
            self.emit(self._status(cycle=previous))


class FakeTransportFactory:
    """A `RobotTransportFactory` that keeps one `FakeTransport` per robot ID for tests to reach."""

    def __init__(self, states: dict[str, dict[str, Any]] | None = None, **defaults: Any) -> None:
        self.states = states or {}
        self.defaults = defaults
        self.transports: dict[str, FakeTransport] = {}

    @classmethod
    def from_fixtures(cls, directory: Path, **defaults: Any) -> "FakeTransportFactory":
        """Seed the i5 and m6 placeholder robots from `<prefix>_state.json` in `directory`."""
        states = {
            rid: state
            for rid, prefix in _FIXTURE_PREFIX.items()
            if (state := _load(directory / f"{prefix}_state.json")) is not None
        }
        return cls(states, **defaults)

    def get(self, robot_id: str) -> FakeTransport:
        """The robot's fake, created on first use so tests can configure it before the service."""
        if robot_id not in self.transports:
            self.transports[robot_id] = FakeTransport(
                robot_id, self.states.get(robot_id), **self.defaults
            )
        return self.transports[robot_id]

    def __call__(self, robot_id: str, address: str, password: str) -> FakeTransport:
        transport = self.get(robot_id)
        transport.address, transport.password = address, password
        return transport


class FakeCloud:
    """A `CloudTransport` serving canned payloads. `fail_everything` raises `cloud_unavailable`."""

    def __init__(
        self,
        *,
        pmaps: dict[str, list[dict[str, Any]]] | None = None,
        umf: dict[tuple[str, str, str], dict[str, Any]] | None = None,
        history: dict[str, list[dict[str, Any]]] | None = None,
        timeline: dict[str, list[dict[str, Any]]] | None = None,
        passwords: dict[str, str] | None = None,
        timeline_delay: float = 0.0,
    ) -> None:
        self.pmaps_by_robot = pmaps or {}
        self.umf_by_key = umf or {}
        self.history_by_robot = history or {}
        self.passwords = passwords or {}
        self.timeline_delay = timeline_delay
        self.fail_everything = False
        self.fail_reason = "connection_failed"
        self.login_error: ErrorCode | None = None
        self.logged_in = False
        self.closed = False
        self.calls: list[str] = []
        self._timeline: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        for blid, reports in (timeline or {}).items():
            for report in reports:
                self.push_timeline(blid, report)

    @classmethod
    def from_fixtures(cls, directory: Path, **kwargs: Any) -> "FakeCloud":
        """Canned i5/m6 data from the `tests/fixtures` files that exist in `directory`."""
        pmaps, history, timeline = {}, {}, {}
        umf: dict[tuple[str, str, str], dict[str, Any]] = {}
        for rid, prefix in _FIXTURE_PREFIX.items():
            if (maps := _load(directory / f"{prefix}_pmaps.json")) is not None:
                pmaps[rid] = maps
            if (records := _load(directory / f"{prefix}_mission_history.json")) is not None:
                history[rid] = records
            if (doc := _load(directory / f"{prefix}_mission_umf.json")) is not None:
                header = doc["maps"][0]["map_header"]
                umf[(rid, header["id"], header["version"])] = doc
            path = directory / f"{prefix}_timeline.jsonl"
            if path.exists():
                timeline[rid] = [json.loads(line) for line in path.read_text().splitlines() if line]
        return cls(pmaps=pmaps, umf=umf, history=history, timeline=timeline, **kwargs)

    def push_timeline(self, blid: str, report: dict[str, Any]) -> None:
        self._queue(blid).put_nowait(report)

    def _queue(self, blid: str) -> asyncio.Queue[dict[str, Any]]:
        return self._timeline.setdefault(blid, asyncio.Queue())

    def _check(self, call: str) -> None:
        self.calls.append(call)
        if self.fail_everything:
            raise KitError(
                ErrorCode.CLOUD_UNAVAILABLE,
                f"fake cloud unavailable: {self.fail_reason}",
                details={"reason": self.fail_reason},
            )

    async def login(self) -> None:
        self._check("login")
        if self.login_error is not None:
            raise KitError(self.login_error, "fake cloud login failed")
        self.logged_in = True

    async def robot_password(self, blid: str) -> str | None:
        self._check("robot_password")
        return self.passwords.get(blid)

    async def pmaps(self, blid: str) -> list[dict[str, Any]]:
        self._check("pmaps")
        return copy.deepcopy(self.pmaps_by_robot.get(blid, []))

    async def umf(self, blid: str, pmap_id: str, version: str) -> dict[str, Any]:
        self._check("umf")
        doc = self.umf_by_key.get((blid, pmap_id, version))
        if doc is None:
            raise KitError(
                ErrorCode.CLOUD_UNAVAILABLE,
                "fake cloud has no such map",
                details={"reason": "request_refused"},
            )
        return copy.deepcopy(doc)

    async def mission_history(self, blid: str) -> list[dict[str, Any]]:
        self._check("mission_history")
        return copy.deepcopy(self.history_by_robot.get(blid, []))

    async def watch_timeline(self, blid: str) -> AsyncIterator[dict[str, Any]]:
        self._check("watch_timeline")
        queue = self._queue(blid)
        while True:
            report = await queue.get()
            await asyncio.sleep(self.timeline_delay)
            self._check("watch_timeline")
            yield report

    async def close(self) -> None:
        self.closed = True


class FakePasswordRetriever:
    """A `PasswordRetriever` answering from a dict of address → password.

    Like the real adapter: an address in `cloud_only` answers without a password
    (`password_unavailable`); any other unknown address doesn't answer (`timeout`).
    """

    def __init__(
        self,
        passwords: dict[str, str] | None = None,
        *,
        error: ErrorCode | None = None,
        delay: float = 0.0,
        cloud_only: Iterable[str] = (),
    ) -> None:
        self.passwords = passwords or {}
        self.error = error
        self.delay = delay
        self.cloud_only = set(cloud_only)
        self.calls: list[str] = []

    async def retrieve(self, address: str, timeout: float) -> str:
        self.calls.append(address)
        if self.delay > timeout:
            await asyncio.sleep(timeout)
            raise KitError(ErrorCode.TIMEOUT, "fake password retrieval timed out")
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise KitError(self.error, f"fake password retrieval failed: {self.error.value}")
        if address in self.cloud_only:
            raise KitError(ErrorCode.PASSWORD_UNAVAILABLE, "fake robot refused: cloud only")
        if address not in self.passwords:
            raise KitError(ErrorCode.TIMEOUT, "fake robot didn't answer")
        return self.passwords[address]
