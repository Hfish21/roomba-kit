"""`credentials.toml`: robot passwords, cloud login and hashed API tokens (contracts/config.md).

The file is owner-only (0600) and written atomically under an advisory lock, so the CLI and
the service can both write it. Reads re-load it whenever it changes on disk. Env overrides
(`ROOMBA_KIT_CLOUD__USERNAME` / `__PASSWORD`, `ROOMBA_KIT_ROBOTS__<BLID>__PASSWORD`) are
layered over the file on read and are never written.
"""

import copy
import fcntl
import os
import re
import stat
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from roomba_kit.config import paths
from roomba_kit.config.settings import dumps_toml
from roomba_kit.core.errors import ErrorCode, KitError

_ROBOT_ENV = re.compile(r"ROOMBA_KIT_ROBOTS__(.+)__PASSWORD", re.IGNORECASE)
_CLOUD_USER_ENV = "ROOMBA_KIT_CLOUD__USERNAME"
_CLOUD_PASSWORD_ENV = "ROOMBA_KIT_CLOUD__PASSWORD"  # noqa: S105 - an env var name


@dataclass(frozen=True)
class CloudCredential:
    username: str
    password: str = field(repr=False)


def _env_robot_passwords() -> dict[str, str]:
    found: dict[str, str] = {}
    for key, value in os.environ.items():
        if (m := _ROBOT_ENV.fullmatch(key)) and value:
            found[m.group(1).upper()] = value
    return found


def check_mode(path: Path, st: os.stat_result | None = None) -> None:
    """Raise unless `path` is accessible by its owner only."""
    st = st or path.stat()
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise KitError(
            ErrorCode.INVALID_REQUEST,
            f"{path} has mode {mode:04o}, which lets other users read secrets. "
            f"Fix it with: chmod 600 {path}",
            details={"path": str(path), "mode": f"{mode:04o}"},
        )


class CredentialStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.credentials_file()
        self._data: dict[str, Any] = {}
        self._stamp: tuple[int, int, int, int] | None = None
        self._loaded = False

    def refresh(self, force: bool = False) -> None:
        """Re-read the file if it changed (mtime, size, inode or mode) since the last read.

        Raises `KitError(invalid_request)` with a `chmod 600` hint if the mode is broader than
        0600, and on every read after that until it's fixed (fail closed).
        """
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._data, self._stamp, self._loaded = {}, None, True
            return
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_mode)
        if self._loaded and not force and stamp == self._stamp:
            return
        check_mode(self.path, st)
        try:
            with self.path.open("rb") as fh:
                data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise KitError(
                ErrorCode.INVALID_REQUEST, f"{self.path} is not valid TOML: {exc}"
            ) from exc
        self._data, self._stamp, self._loaded = data, stamp, True

    @contextmanager
    def edit(self) -> Iterator[dict[str, Any]]:
        """Lock, re-read, yield the raw document for mutation, then write it back atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.refresh(force=True)
            data = copy.deepcopy(self._data)
            yield data
            paths.atomic_write(self.path, dumps_toml(data), mode=0o600)
            self.refresh(force=True)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _snapshot(self) -> dict[str, Any]:
        self.refresh()
        return self._data

    # Robots

    def robot_password(self, robot_id: str) -> str | None:
        if env := _env_robot_passwords().get(robot_id.upper()):
            return env
        entry = self._snapshot().get("robots", {}).get(robot_id, {})
        password = entry.get("password")
        return password if isinstance(password, str) and password else None

    def set_robot_password(self, robot_id: str, password: str) -> None:
        with self.edit() as data:
            data.setdefault("robots", {}).setdefault(robot_id, {})["password"] = password

    def remove_robot(self, robot_id: str) -> bool:
        with self.edit() as data:
            return data.get("robots", {}).pop(robot_id, None) is not None

    # Cloud

    def cloud(self) -> CloudCredential | None:
        """Cloud login; each field can be overridden by env independently."""
        file = self._snapshot().get("cloud", {})
        username = os.environ.get(_CLOUD_USER_ENV) or file.get("username")
        password = os.environ.get(_CLOUD_PASSWORD_ENV) or file.get("password")
        if not username or not password:
            return None
        return CloudCredential(str(username), str(password))

    def set_cloud(self, username: str, password: str) -> None:
        with self.edit() as data:
            data["cloud"] = {"username": username, "password": password}

    def clear_cloud(self) -> bool:
        with self.edit() as data:
            return data.pop("cloud", None) is not None

    # Tokens (records are managed by tokens.TokenStore)

    def token_records(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._snapshot().get("tokens", [])]

    def secrets(self) -> Iterator[str]:
        """Every secret value currently known, from env and file. Used by the log redactor."""
        yield from _env_robot_passwords().values()
        for var in (_CLOUD_USER_ENV, _CLOUD_PASSWORD_ENV):
            if value := os.environ.get(var):
                yield value
        data = self._snapshot()
        for entry in data.get("robots", {}).values():
            if isinstance(entry, dict) and (pw := entry.get("password")):
                yield str(pw)
        cloud = data.get("cloud", {})
        for key in ("username", "password"):
            if value := cloud.get(key):
                yield str(value)
