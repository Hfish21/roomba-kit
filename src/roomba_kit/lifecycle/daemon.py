"""The detached background service: spawn, pidfile, health polling, stop.

Process-level only: the command to run is injected, so this is testable without uvicorn.
"""

import os
import signal
import subprocess
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import IO, Any

import httpx

from roomba_kit.config.paths import atomic_write


class DaemonError(Exception):
    pass


class AlreadyRunning(DaemonError):
    def __init__(self, pid: int) -> None:
        super().__init__(f"Service already running (pid {pid}).")
        self.pid = pid


def alive(pid: int) -> bool:
    try:  # Reap it first if it's our own exited child, so a zombie doesn't count as running.
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pid(pid_path: Path) -> int | None:
    try:
        return int(pid_path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def running_pid(pid_path: Path) -> int | None:
    """The pidfile's pid if that process is alive; a stale pidfile is removed."""
    pid = read_pid(pid_path)
    if pid is not None and alive(pid):
        return pid
    pid_path.unlink(missing_ok=True)
    return None


def health(url: str, timeout: float = 1.0) -> dict[str, Any] | None:
    """The `GET /v1/health` body if the service at `url` answers 200, else None."""
    try:
        response = httpx.get(url.rstrip("/") + "/v1/health", timeout=timeout)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def tail(path: Path, lines: int = 20) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def follow(path: Path, poll_s: float = 0.5) -> Iterator[str]:
    """Lines appended to `path` from now on, forever."""
    fh = path.open(encoding="utf-8", errors="replace")
    fh.seek(0, os.SEEK_END)

    def lines(fh: IO[str]) -> Iterator[str]:
        with fh:
            while True:
                if line := fh.readline():
                    yield line
                else:
                    time.sleep(poll_s)

    return lines(fh)


def _signal_and_wait(pid: int, timeout: float) -> None:
    """SIGTERM, wait up to `timeout`, then SIGKILL."""
    for sig, wait in ((signal.SIGTERM, timeout), (signal.SIGKILL, 5.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not alive(pid):
                return
            time.sleep(0.05)
    if alive(pid):
        raise DaemonError(f"pid {pid} survived SIGKILL")


def start(
    command: Sequence[str],
    *,
    pid_path: Path,
    log_path: Path,
    url: str,
    timeout: float = 10.0,
    poll_s: float = 0.1,
) -> int:
    """Spawn `command` detached, write the pidfile, and wait until `url` reports healthy."""
    if (pid := running_pid(pid_path)) is not None:
        raise AlreadyRunning(pid)
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        proc = subprocess.Popen(  # noqa: S603 - the kit's own serve command
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(log_fd)
    atomic_write(pid_path, f"{proc.pid}\n")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (code := proc.poll()) is not None:
            pid_path.unlink(missing_ok=True)
            raise DaemonError(f"Service exited with code {code}. Log:\n{tail(log_path)}")
        if health(url, timeout=min(1.0, timeout)) is not None:
            proc.returncode = 0  # Detached on purpose: never waited on, so don't warn about it.
            return proc.pid
        time.sleep(poll_s)
    proc.terminate()
    try:
        proc.wait(2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    pid_path.unlink(missing_ok=True)
    raise DaemonError(f"Service wasn't healthy after {timeout:g} s. Log:\n{tail(log_path)}")


def stop(pid_path: Path, timeout: float = 10.0) -> int | None:
    """Stop the pidfile's process (SIGTERM, then SIGKILL after `timeout`). None if not running."""
    pid = running_pid(pid_path)
    if pid is None:
        return None
    _signal_and_wait(pid, timeout)
    pid_path.unlink(missing_ok=True)
    return pid
