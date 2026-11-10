"""API bearer tokens (research R6). Only SHA-256 digests are stored, in `credentials.toml`.

Revocation from another process takes effect on the next `verify`, because the credential
store re-reads the file when it changes. `last_used_at` is kept in memory and persisted at most
once per `persist_every_s` per token, so authenticated requests don't rewrite the file.
"""

import builtins
import hashlib
import hmac
import os
import re
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from roomba_kit.config import paths
from roomba_kit.config.credentials import CredentialStore, check_mode
from roomba_kit.core.errors import ErrorCode, KitError

CLI_TOKEN_NAME = "cli"  # noqa: S105 - a token name, not a token
TOKEN_PREFIX = "rk_"  # noqa: S105 - lets the redactor recognise kit tokens by shape
API_TOKEN_ENV = "ROOMBA_KIT_API_TOKEN"  # noqa: S105 - an env var name
ENV_TOKEN_NAME = "env"  # noqa: S105 - the name the service gives `ROOMBA_KIT_API_TOKEN`
ENV_TOKEN_MIN_LEN = 16
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


@dataclass(frozen=True)
class TokenInfo:
    name: str
    created_at: datetime
    last_used_at: datetime | None


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _fmt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


def _read_token_file(path: Path, *, check: bool = True) -> str | None:
    try:
        if check:
            check_mode(path)  # FR-013: a plaintext token readable by others is refused
        return path.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def read_cli_token(path: Path | None = None) -> str | None:
    """The token the local CLI and stdio MCP send: `ROOMBA_KIT_API_TOKEN`, else `cli.token`.

    Raises `invalid_request` with a `chmod 600` hint if `cli.token` is broader than 0600.
    """
    if env := os.environ.get(API_TOKEN_ENV):
        return env
    return _read_token_file(path or paths.cli_token_file())


class TokenStore:
    def __init__(
        self,
        credentials: CredentialStore,
        cli_token_path: Path | None = None,
        persist_every_s: float = 300.0,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._credentials = credentials
        self._cli_token_path = cli_token_path or paths.cli_token_file()
        self._persist_every = timedelta(seconds=persist_every_s)
        self._clock = clock
        self._pending: dict[str, datetime] = {}
        self._persisted_at: dict[str, datetime] = {}
        self._issued: list[str] = []

    def create(self, name: str, *, replace: bool = False) -> str:
        """Mint a token and return its plaintext. This is the only time it's available."""
        if not _NAME.fullmatch(name):
            raise KitError(ErrorCode.INVALID_REQUEST, f"invalid token name {name!r}")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._credentials.edit() as data:
            records = data.setdefault("tokens", [])
            if any(r.get("name") == name for r in records):
                if not replace:
                    raise KitError(
                        ErrorCode.INVALID_REQUEST, f"a token named {name!r} already exists"
                    )
                records[:] = [r for r in records if r.get("name") != name]
            records.append(
                {"name": name, "sha256": _digest(token), "created_at": _fmt(self._clock())}
            )
        if name == CLI_TOKEN_NAME:
            paths.atomic_write(self._cli_token_path, token + "\n", mode=0o600)
        self._issued.append(token)
        return token

    def verify(self, token: str) -> str | None:
        """The token's name if it's valid, else None.

        `ROOMBA_KIT_API_TOKEN` in the service's environment is also accepted, as `env`, so a
        container can run from env alone (FR-010).
        """
        env = os.environ.get(API_TOKEN_ENV, "")
        if len(env) >= ENV_TOKEN_MIN_LEN and hmac.compare_digest(token, env):
            return ENV_TOKEN_NAME
        digest = _digest(token)
        name: str | None = None
        for record in self._credentials.token_records():
            if hmac.compare_digest(str(record.get("sha256", "")), digest):
                name = str(record["name"])
        if name is not None:
            self._touch(name)
        return name

    def _touch(self, name: str) -> None:
        now = self._clock()
        self._pending[name] = now
        last = self._persisted_at.get(name)
        if last is None or now - last >= self._persist_every:
            self.flush()

    def flush(self) -> None:
        """Persist pending `last_used_at` values. Call on shutdown."""
        if not self._pending:
            return
        pending, self._pending = self._pending, {}
        with self._credentials.edit() as data:
            for record in data.get("tokens", []):
                if (used := pending.get(record.get("name", ""))) is not None:
                    previous = _parse(record.get("last_used_at"))
                    record["last_used_at"] = _fmt(max(used, previous) if previous else used)
        now = self._clock()
        for name in pending:
            self._persisted_at[name] = now

    def list(self) -> builtins.list[TokenInfo]:
        """Name and timestamps only; digests are never returned."""
        infos: builtins.list[TokenInfo] = []
        for record in self._credentials.token_records():
            name = str(record["name"])
            used = [t for t in (_parse(record.get("last_used_at")), self._pending.get(name)) if t]
            created = _parse(record.get("created_at")) or datetime.fromtimestamp(0, UTC)
            infos.append(TokenInfo(name, created, max(used) if used else None))
        return infos

    def revoke(self, name: str) -> bool:
        with self._credentials.edit() as data:
            records = data.get("tokens", [])
            kept = [r for r in records if r.get("name") != name]
            found = len(kept) != len(records)
            data["tokens"] = kept
        self._pending.pop(name, None)
        if name == CLI_TOKEN_NAME:
            self._cli_token_path.unlink(missing_ok=True)
        return found

    def check(self) -> None:
        """Raise unless `cli.token` (when present) is 0600 and any `ROOMBA_KIT_API_TOKEN` is
        long enough to accept. The service runs this at start."""
        _read_token_file(self._cli_token_path)
        env = os.environ.get(API_TOKEN_ENV, "")
        if env and len(env) < ENV_TOKEN_MIN_LEN:
            raise KitError(
                ErrorCode.INVALID_REQUEST,
                f"{API_TOKEN_ENV} is too short for the service to accept: use at least "
                f"{ENV_TOKEN_MIN_LEN} characters (`roomba-kit token create <name>` makes one).",
            )

    def secrets(self) -> Iterator[str]:
        """Plaintexts this process knows: tokens it minted, plus the CLI and env tokens.

        Reads `cli.token` whatever its mode: masking it in logs matters most when it's exposed.
        """
        yield from self._issued
        if env := os.environ.get(API_TOKEN_ENV):
            yield env
        if cli := _read_token_file(self._cli_token_path, check=False):
            yield cli
