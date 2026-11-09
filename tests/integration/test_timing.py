"""Timing success criteria (T109): SC-003, SC-005, SC-006 and SC-009, against fakes.

How time is handled, per criterion:
- SC-003 (confirmation p95 ≤ 10 s): real HTTP calls against fakes whose reaction delays are drawn
  from distributions based on the hardware observations in docs/research/discovery-2026-11-08.md
  §2, divided by `SCALE`. Measured wall time is multiplied back by `SCALE`, so the kit's own
  overhead is inflated `SCALE`-fold: the reported p95 is an upper bound, never an underestimate.
- SC-005 (robot report → SSE frame ≤ 2 s): real time, real coalescing (1 s). Nothing is scaled.
- SC-006 (reconnect ≤ 60 s after the robot is back): `RobotSession` with its real backoff
  (1 s doubling to 60 s) and the service's real rediscovery call, on a virtual-time event loop
  (`helpers.VirtualTimeLoop`): every timer the session sets runs at its real length, but the
  clock jumps when all tasks are waiting, so minutes of outage take milliseconds.
- SC-009 (`service start` ready ≤ 10 s): the real `roomba-kit service start` in a subprocess.
"""

import asyncio
import contextlib
import json
import math
import os
import random
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from roomba_kit.config import paths
from roomba_kit.config.settings import RobotEntry, load_settings
from roomba_kit.core.models import ConnectionState, DiscoveredRobot
from roomba_kit.profiles.loader import load_profiles
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.session import RobotSession
from roomba_kit.transport.fake import FakeTransport, FakeTransportFactory
from tests.integration.helpers import (
    FAST_BACKOFF,
    FIXTURES,
    I5,
    M6,
    api_client,
    configure_robots,
    http_call,
    run_virtual,
    serving,
)
from tests.unit.test_sse import Stream

# -- SC-003: command confirmation ------------------------------------------------------------

SCALE = 40
"""Fake reactions run 40x faster than the robot; measured latencies are multiplied by 40."""
CYCLE = ("clean", "pause", "resume", "dock", "find")
ROUNDS = 6


def reaction_s(rng: random.Random, command: str) -> float:
    """A robot's reaction time for `command`, in robot seconds (discovery §2 observations)."""
    if command == "clean":  # phase charge→run in ~5 s
        return rng.lognormvariate(math.log(5.0), 0.3)
    if command in ("pause", "resume"):  # usually < 1 s; the m6 once took ~5 s
        late = rng.random() < 0.1
        return rng.lognormvariate(math.log(5.0 if late else 0.7), 0.4)
    if command == "dock":  # hmUsrDock (returning) shows within a couple of seconds
        return rng.lognormvariate(math.log(1.5), 0.5)
    return rng.lognormvariate(math.log(0.8), 0.4)  # find: echo within ~1 s


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


async def test_sc003_command_confirmation_p95_within_10s(tmp_kit_home):
    configure_robots()
    factory = FakeTransportFactory.from_fixtures(FIXTURES, dock_delay=0.0, evac_duration=0.0)
    ctx = ServiceContext(
        settings=load_settings(), transport_factory=factory, session_options=FAST_BACKOFF
    )
    rng = random.Random(2026)  # noqa: S311 - a reproducible test distribution
    measured: list[tuple[str, float, float]] = []  # (command, injected s, measured s)

    async with serving(ctx) as app, api_client(app, ctx.tokens.create("t")) as client:

        async def drive(robot: str) -> None:
            fake = factory.get(robot)
            for _ in range(ROUNDS):
                for command in CYCLE:
                    injected = reaction_s(rng, command)
                    fake.reaction_delay = injected / SCALE
                    started = time.monotonic()
                    response = await http_call(client, command, {"robot": robot})
                    took = (time.monotonic() - started) * SCALE
                    assert response.json()["outcome"] == "confirmed", (command, response.text)
                    measured.append((command, injected, took))

        await asyncio.gather(drive(I5), drive(M6))

    assert len(measured) == 2 * ROUNDS * len(CYCLE)
    # The measurement includes the robot's reaction: it can't come in under the injected delay.
    assert all(took >= injected for _, injected, took in measured)
    injected_p95 = p95([i for _, i, _ in measured])
    assert injected_p95 > 4.0, "the distribution must stay realistic, not trivially fast"
    assert p95([t for *_, t in measured]) <= 10.0, sorted(t for *_, t in measured)[-6:]


# -- SC-005: event latency -------------------------------------------------------------------


async def test_sc005_robot_report_reaches_sse_within_2s(tmp_kit_home):
    configure_robots()
    factory = FakeTransportFactory.from_fixtures(FIXTURES)
    ctx = ServiceContext(  # default coalescing (1 s): the slowest path state.changed can take
        settings=load_settings(), transport_factory=factory, session_options=FAST_BACKOFF
    )
    fake = factory.get(I5)
    latencies: dict[str, list[float]] = {}

    async with serving(ctx) as app:
        token = ctx.tokens.create("sse")
        async with Stream(app, token, f"robot={I5}", accept="text/event-stream") as stream:

            async def measure(kind: str, report: Any, match: Any) -> None:
                sent = time.monotonic()
                report()
                while True:
                    frame = await stream.frame(timeout=5.0)
                    if frame["event"] == kind and match(json.loads(frame["data"])["data"]):
                        latencies.setdefault(kind, []).append(time.monotonic() - sent)
                        return

            status = fake.reported["cleanMissionStatus"]
            for code in (18, 0, 19, 0):
                kind = "error.raised" if code else "error.cleared"
                await measure(
                    kind,
                    lambda c=code: fake.emit({"cleanMissionStatus": {**status, "error": c}}),
                    lambda data: True,
                )
            # Worst case for state.changed: a change right after a frame waits out the coalescing.
            for pct in (41, 42, 43):
                await measure(
                    "state.changed",
                    lambda p=pct: fake.emit({"batPct": p}),
                    lambda data, p=pct: data["state"]["battery_pct"] == p,
                )
            await measure(
                "connection.changed",
                lambda: fake.drop(offline=False),
                lambda data: data["from"] == "connected",
            )

    assert set(latencies) == {
        "error.raised",
        "error.cleared",
        "state.changed",
        "connection.changed",
    }
    worst = max(max(v) for v in latencies.values())
    assert worst <= 2.0, latencies
    # Coalescing really was in play, so the bound was tested at its worst case.
    assert max(latencies["state.changed"]) > 0.5, latencies


# -- SC-006: reconnect after an outage -------------------------------------------------------


class Bus:
    async def publish(self, *args: Any) -> None:
        pass


async def outage(restore_after_failure: int, then_wait: float, *, rediscovery: bool) -> float:
    """Seconds from the robot becoming reachable to the session reporting `connected`.

    The robot goes offline, the session fails `restore_after_failure` connects, then the robot
    comes back `then_wait` seconds after that failure. Backoff is the session's real default.
    """
    loop = asyncio.get_running_loop()

    async def discover(broadcast: str, timeout_s: float) -> list[DiscoveredRobot]:
        await asyncio.sleep(timeout_s)  # a broadcast always listens for its whole window
        return []

    service = ServiceContext(settings=load_settings(), discover=discover)
    factory = FakeTransportFactory.from_fixtures(FIXTURES)
    fake: FakeTransport = factory.get(I5)
    failures: asyncio.Queue[float] = asyncio.Queue()
    connected = asyncio.Event()
    real_connect = fake.connect

    async def connect() -> None:
        try:
            await real_connect()
        except Exception:
            failures.put_nowait(loop.time())
            raise
        connected.set()

    fake.connect = connect  # type: ignore[method-assign]
    entry = RobotEntry(id=I5, name="Vacuum", address="192.0.2.10", profile="roomba_i5")
    session = RobotSession(  # no backoff options: the real 1 s → 60 s schedule
        entry,
        load_profiles()["roomba_i5"],
        factory,
        lambda: "pw",
        Bus(),  # type: ignore[arg-type]
        rediscover=service.rediscover if rediscovery else None,
    )
    await session.start()
    try:
        await connected.wait()
        connected.clear()
        fake.drop()
        for _ in range(restore_after_failure):
            await failures.get()
        await asyncio.sleep(then_wait)
        back = loop.time()
        fake.restore()
        await connected.wait()
        assert session.connection is ConnectionState.CONNECTED
        assert session.backoff_history[:8] == [1, 2, 4, 8, 16, 32, 60, 60]
        return loop.time() - back
    finally:
        await session.stop()


# Restore points across two full 3-attempt cycles once backoff has reached its 60 s cap, at the
# start, middle and very end of each wait.
RESTORE_POINTS = [(k, wait) for k in range(7, 13) for wait in (0.0, 4.0, 30.0, 59.5)]


def test_sc006_backoff_alone_reconnects_within_60s(tmp_kit_home):
    async def worst() -> float:
        return max([await outage(k, w, rediscovery=False) for k, w in RESTORE_POINTS])

    latency = run_virtual(worst)
    assert 59.0 < latency <= 60.0  # the cap itself is the worst case


@pytest.mark.xfail(
    strict=True,
    reason="FR-018 rediscovery: every 3rd failed connect runs an 8 s discovery broadcast before "
    "the 60 s backoff, so a robot returning then waits 68 s (session._attempt).",
)
def test_sc006_reconnects_within_60s_with_rediscovery(tmp_kit_home):
    async def worst() -> float:
        return max([await outage(k, w, rediscovery=True) for k, w in RESTORE_POINTS])

    assert run_virtual(worst) <= 60.0


# -- SC-009: service readiness ---------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_sc009_service_start_ready_within_10s(tmp_kit_home):
    port = free_port()
    paths.config_file().write_text(f"[server]\nport = {port}\n")  # zero robots
    exe = str(Path(sys.executable).parent / "roomba-kit")
    health = f"http://127.0.0.1:{port}/v1/health"
    started = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603 - our own console script
        [exe, "--json", "service", "start", "--timeout", "30"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = None
        while ready is None and time.monotonic() - started < 30:
            with contextlib.suppress(httpx.TransportError):
                if httpx.get(health, timeout=0.5).status_code == 200:
                    ready = time.monotonic() - started
            time.sleep(0.02)
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err.decode()
        assert json.loads(out)["started"] is True
        assert ready is not None and ready <= 10.0, ready
    finally:
        subprocess.run([exe, "service", "stop"], capture_output=True, timeout=30)  # noqa: S603
        if proc.poll() is None:
            proc.kill()
        with contextlib.suppress(FileNotFoundError, ValueError, ProcessLookupError):
            os.kill(int(paths.pid_file().read_text().strip()), signal.SIGKILL)
    with pytest.raises(httpx.TransportError):
        httpx.get(health, timeout=0.5)
