"""Config and state locations (contracts/config.md, research R5) and atomic file writes."""

import os
import secrets
from pathlib import Path

APP = "roomba-kit"


def _xdg(var: str, fallback: str) -> Path:
    base = os.environ.get(var)
    return Path(base) / APP if base else Path.home() / fallback / APP


def config_dir() -> Path:
    if override := os.environ.get("ROOMBA_KIT_CONFIG_DIR"):
        return Path(override)
    return _xdg("XDG_CONFIG_HOME", ".config")


def state_dir() -> Path:
    if override := os.environ.get("ROOMBA_KIT_STATE_DIR"):
        return Path(override)
    return _xdg("XDG_STATE_HOME", ".local/state")


def config_file() -> Path:
    return config_dir() / "config.toml"


def credentials_file() -> Path:
    return config_dir() / "credentials.toml"


def state_db() -> Path:
    return state_dir() / "state.db"


def cli_token_file() -> Path:
    return state_dir() / "cli.token"


def pid_file() -> Path:
    return state_dir() / "service.pid"


def log_file() -> Path:
    return state_dir() / "service.log"


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    """Write `text` to a temp file created with `mode`, fsync it, then rename over `path`.

    The temp file never exists with broader permissions than `mode`, so a 0600 file is never
    briefly readable by others. Parent directories are created owner-only.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
