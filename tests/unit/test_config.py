import tomllib
from pathlib import Path

import pytest

from roomba_kit.config import paths
from roomba_kit.config.settings import (
    ConfigModel,
    RobotEntry,
    dumps_toml,
    load_config,
    load_settings,
    remove_robot,
    save_config,
    upsert_robot,
)

BLID = "00000000000000000000000000000001"
CANARY = "CANARY-robot-pw-7f3a"
CLOUD_CANARY = "CANARY-cloud-pw-91bd"


# Paths


def test_paths_use_overrides(tmp_kit_home: Path) -> None:
    assert paths.config_dir() == tmp_kit_home / "config"
    assert paths.state_dir() == tmp_kit_home / "state"
    assert paths.credentials_file() == tmp_kit_home / "config" / "credentials.toml"
    assert paths.cli_token_file() == tmp_kit_home / "state" / "cli.token"
    assert paths.state_db().name == "state.db"


def test_paths_fall_back_to_xdg_then_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("ROOMBA_KIT_CONFIG_DIR", "ROOMBA_KIT_STATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xc"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert paths.config_dir() == tmp_path / "xc" / "roomba-kit"
    assert paths.state_dir() == tmp_path / "home" / ".local" / "state" / "roomba-kit"


# Settings


def test_defaults_without_a_file(tmp_kit_home: Path) -> None:
    s = load_settings()
    assert (s.server.host, s.server.port) == ("127.0.0.1", 8765)
    assert s.alerts.dock_search_minutes == 5
    assert s.history.event_retention_days == 30
    assert s.events.persist_position is False
    assert s.robots == []


def test_env_beats_file_beats_defaults(tmp_kit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths.config_file().write_text('[server]\nhost = "0.0.0.0"\nport = 9000\n[alerts]\n')
    monkeypatch.setenv("ROOMBA_KIT_SERVER__PORT", "9100")
    monkeypatch.setenv(f"ROOMBA_KIT_ROBOTS__{BLID}__PASSWORD", CANARY)
    monkeypatch.setenv("ROOMBA_KIT_CLOUD__PASSWORD", CLOUD_CANARY)
    s = load_settings()
    assert s.server.host == "0.0.0.0"  # noqa: S104 - file value under test
    assert s.server.port == 9100
    assert s.alerts.dock_search_minutes == 5
    assert CANARY not in s.model_dump_json() and CLOUD_CANARY not in s.model_dump_json()


def test_env_values_are_never_written_back(
    tmp_kit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths.config_file().write_text("[server]\nport = 9000\n")
    monkeypatch.setenv("ROOMBA_KIT_SERVER__HOST", "192.0.2.99")
    upsert_robot(RobotEntry(id=BLID, name="Kitchen", address="192.0.2.10", profile="roomba_i5"))
    written = tomllib.loads(paths.config_file().read_text())
    assert set(written) == {"server", "robots"}
    assert written["server"] == {"port": 9000}
    assert written["robots"][0]["id"] == BLID
    with pytest.raises(TypeError):
        save_config(load_settings())


def test_robot_helpers_round_trip(tmp_kit_home: Path) -> None:
    upsert_robot(RobotEntry(id=BLID, name="Kitchen"))
    upsert_robot(RobotEntry(id=BLID, name="Hall", address="192.0.2.11"))
    entry = load_config().robot(BLID)
    assert entry is not None and (entry.name, entry.address) == ("Hall", "192.0.2.11")
    assert remove_robot(BLID) and not remove_robot(BLID)
    assert load_config().robots == []


def test_toml_writer_round_trips_kit_shapes() -> None:
    data = {
        "server": {"host": "127.0.0.1", "port": 8765},
        "events": {"persist_position": True},
        "robots": {BLID: {"password": 'q"uo\\te\nnew'}},
        "tokens": [{"name": "cli", "sha256": "ab"}, {"name": "hermes", "sha256": "cd"}],
        "list": [1, 2.5, "x"],
    }
    assert tomllib.loads(dumps_toml(data)) == data
    assert ConfigModel.model_validate(tomllib.loads(dumps_toml({}))) == ConfigModel()
