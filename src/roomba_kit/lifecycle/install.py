"""Start-on-boot installs (research R11, FR-015): what to run, the template renderer, the runner.

The supervisor backends are `launchd.py` (macOS) and `systemd.py` (Linux). Both take the
command runner and their target path as arguments, so tests never touch the real supervisor
or home directory.
"""

import os
import re
import string
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Protocol

from roomba_kit.core.errors import ErrorCode

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class InstallError(Exception):
    def __init__(self, message: str, code: ErrorCode = ErrorCode.INTERNAL) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ServiceSpec:
    """What the supervisor runs: argv, extra environment, and the log both streams append to."""

    command: tuple[str, ...]
    env: Mapping[str, str]
    log_path: Path

    def values(self) -> list[str]:
        return [*self.command, *self.env, *self.env.values(), str(self.log_path)]


@dataclass(frozen=True)
class Result:
    returncode: int
    output: str


Runner = Callable[[Sequence[str]], Result]


class ServiceManager(Protocol):
    name: str
    path: Path

    def render(self, spec: ServiceSpec) -> str: ...
    def install(self, spec: ServiceSpec) -> list[str]: ...
    def uninstall(self) -> bool: ...


def run_command(args: Sequence[str]) -> Result:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed supervisor commands, no shell
            list(args), capture_output=True, text=True, check=False, timeout=60
        )
    except FileNotFoundError:
        raise InstallError(
            f"{args[0]} not found; this host has no supported service manager.",
            ErrorCode.NOT_SUPPORTED,
        ) from None
    return Result(proc.returncode, (proc.stdout + proc.stderr).strip())


def check(result: Result, args: Sequence[str]) -> Result:
    if result.returncode:
        raise InstallError(f"`{' '.join(args)}` failed ({result.returncode}): {result.output}")
    return result


def render_text(template: str, values: Mapping[str, str]) -> str:
    """`string.Template` substitution that fails on a missing *or* an unused value."""
    tmpl = string.Template(template)
    if unused := set(values) - set(tmpl.get_identifiers()):
        raise ValueError(f"unused template values: {sorted(unused)}")
    return tmpl.substitute(values)


def render_template(name: str, values: Mapping[str, str]) -> str:
    text = files("roomba_kit.lifecycle").joinpath("templates", name).read_text(encoding="utf-8")
    return render_text(text, values)


def refuse_control_chars(spec: ServiceSpec) -> None:
    """A newline in a value would let it inject unit/plist lines; nothing legitimate has one."""
    for value in spec.values():
        if _CONTROL.search(value):
            raise InstallError(f"{value!r} contains a control character", ErrorCode.INVALID_REQUEST)


def prepare_log(path: Path) -> None:
    """Create the state dir and an owner-only log before the supervisor opens it (as `0644`)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
