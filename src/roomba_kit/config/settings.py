"""Non-secret settings from `config.toml` with `ROOMBA_KIT_*` env overrides (contracts/config.md).

Two views of the same schema:
- `Settings` is the effective, read-only view: env, then the file, then defaults.
- `ConfigModel` is the file's own content (file, then defaults). It is the only thing
  `save_config` accepts, so env-sourced values can never be written back.
"""

import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from roomba_kit.config import paths
from roomba_kit.core.errors import ErrorCode, KitError


class _Section(BaseModel):
    # `ignore`, not `forbid`: `ROOMBA_KIT_CLOUD__PASSWORD` lands in the `cloud` section's env
    # input and must be dropped here (credentials.py owns it).
    model_config = ConfigDict(extra="ignore")


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)


class AlertsConfig(_Section):
    dock_search_minutes: int = Field(default=5, ge=1)


class HistoryConfig(_Section):
    event_retention_days: int = Field(default=30, ge=1)


class EventsConfig(_Section):
    persist_position: bool = False


class CloudConfig(_Section):
    enabled: bool = True
    country: str = "US"


class RobotEntry(_Section):
    id: str = Field(min_length=1, description="BLID; the only identifier")
    name: str = ""
    address: str | None = Field(default=None, description="Last known IP; a hint")
    profile: str | None = None
    sku: str | None = Field(default=None, description="From discovery; refreshed from the robot")
    firmware: str | None = None


class DiscoveryConfig(_Section):
    broadcast: str = Field(
        default="255.255.255.255",
        description="Broadcast address for rediscovery; use the subnet's (e.g. 192.0.2.255) "
        "when a VPN owns the default route",
    )


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    server: ServerConfig = Field(default_factory=ServerConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    robots: list[RobotEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_robot_ids(self) -> "ConfigModel":
        ids = [r.id for r in self.robots]
        if len(ids) != len(set(ids)):
            raise ValueError("robot ids in config.toml must be unique")
        return self

    def robot(self, robot_id: str) -> RobotEntry | None:
        return next((r for r in self.robots if r.id == robot_id), None)


class _EnvSource(EnvSettingsSource):
    """Env overrides minus `robots`: `ROOMBA_KIT_ROBOTS__<BLID>__PASSWORD` is a credential,
    and the robot list itself is file-only."""

    def __call__(self) -> dict[str, Any]:
        data = super().__call__()
        data.pop("robots", None)
        return data


class Settings(BaseSettings, ConfigModel):
    model_config = SettingsConfigDict(
        env_prefix="ROOMBA_KIT_", env_nested_delimiter="__", extra="ignore"
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # File content arrives as init kwargs; env outranks it, deep-merged per section.
        return (_EnvSource(settings_cls), init_settings)


def read_config_file(path: Path | None = None) -> dict[str, Any]:
    """The raw contents of `config.toml`, or `{}` when it doesn't exist."""
    path = path or paths.config_file()
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise KitError(ErrorCode.INVALID_REQUEST, f"{path} is not valid TOML: {exc}") from exc


def load_settings(path: Path | None = None) -> Settings:
    """Effective settings: env, then `config.toml`, then defaults."""
    return Settings(**read_config_file(path))


def load_config(path: Path | None = None) -> ConfigModel:
    """The file's own values (plus defaults), with no env applied. Edit this, then save it."""
    return ConfigModel.model_validate(read_config_file(path))


def save_config(config: ConfigModel, path: Path | None = None) -> None:
    """Atomically write `config`. Values equal to the defaults are omitted."""
    if isinstance(config, Settings):
        raise TypeError("save_config takes load_config()'s result; Settings carries env values")
    data = config.model_dump(mode="json", exclude_defaults=True)
    paths.atomic_write(path or paths.config_file(), dumps_toml(data), mode=0o644)


def upsert_robot(entry: RobotEntry, path: Path | None = None) -> ConfigModel:
    """Add `entry`, or replace the entry with the same id in place."""
    config = load_config(path)
    robots = [entry if r.id == entry.id else r for r in config.robots]
    if config.robot(entry.id) is None:
        robots.append(entry)
    config.robots = robots
    save_config(config, path)
    return config


def remove_robot(robot_id: str, path: Path | None = None) -> bool:
    config = load_config(path)
    kept = [r for r in config.robots if r.id != robot_id]
    if len(kept) == len(config.robots):
        return False
    config.robots = kept
    save_config(config, path)
    return True


# A minimal TOML writer for the kit's own shapes: tables, arrays of tables, and scalars or
# lists of scalars (str, int, float, bool). No TOML writer ships with the stdlib, and this
# avoids a runtime dependency. `None` values are omitted.

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else json.dumps(key, ensure_ascii=False)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        # JSON string escapes are a subset of TOML basic-string escapes.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    raise TypeError(f"can't write {type(value).__name__} to TOML")


def _is_table_array(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(v, Mapping) for v in value)


def _table(data: Mapping[str, Any], path: tuple[str, ...], out: list[str]) -> None:
    for k, v in data.items():
        if v is not None and not isinstance(v, Mapping) and not _is_table_array(v):
            out.append(f"{_key(k)} = {_scalar(v)}")
    for k, v in data.items():
        header = ".".join(_key(p) for p in (*path, k))
        if isinstance(v, Mapping):
            out += ["", f"[{header}]"]
            _table(v, (*path, k), out)
        elif _is_table_array(v):
            for item in v:
                out += ["", f"[[{header}]]"]
                _table(item, (*path, k), out)


def dumps_toml(data: Mapping[str, Any]) -> str:
    out: list[str] = []
    _table(data, (), out)
    return "\n".join(out).lstrip("\n") + "\n"
