import json
import logging
import os
import stat
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from roomba_kit.config import paths
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.redact import RedactFilter, install_redaction
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
from roomba_kit.config.tokens import TokenStore, read_cli_token
from roomba_kit.core.errors import ErrorCode, KitError

BLID = "00000000000000000000000000000001"
CANARY = "CANARY-robot-pw-7f3a"
CLOUD_CANARY = "CANARY-cloud-pw-91bd"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


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


def test_robots_from_env_merge_by_id_and_are_never_written(
    tmp_kit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-010: a container can define its robots in `ROOMBA_KIT_ROBOTS` (JSON)."""
    other = "00000000000000000000000000000002"
    paths.config_file().write_text(
        f'[[robots]]\nid = "{BLID}"\nname = "Kitchen"\naddress = "192.0.2.10"\n'
    )
    env = [
        {"id": BLID, "address": "192.0.2.20"},
        {"id": other, "name": "Mop", "address": "192.0.2.11", "profile": "braava_m6"},
    ]
    monkeypatch.setenv("ROOMBA_KIT_ROBOTS", json.dumps(env))
    monkeypatch.setenv(f"ROOMBA_KIT_ROBOTS__{BLID}__PASSWORD", CANARY)  # still a credential
    robots = {r.id: r for r in load_settings().robots}
    assert (robots[BLID].name, robots[BLID].address) == ("Kitchen", "192.0.2.20")
    assert (robots[other].name, robots[other].profile) == ("Mop", "braava_m6")
    assert CredentialStore().robot_password(BLID) == CANARY
    assert [r.id for r in load_config().robots] == [BLID]
    upsert_robot(RobotEntry(id=BLID, name="Hall", address="192.0.2.10"))
    written = tomllib.loads(paths.config_file().read_text())
    assert [(r["id"], r["address"]) for r in written["robots"]] == [(BLID, "192.0.2.10")]

    for bad in ("not json", '{"id": "x"}', '[{"name": "no id"}]'):
        monkeypatch.setenv("ROOMBA_KIT_ROBOTS", bad)
        with pytest.raises(KitError) as err:
            load_settings()
        assert (
            err.value.code is ErrorCode.INVALID_REQUEST and "ROOMBA_KIT_ROBOTS" in err.value.message
        )


def test_service_accepts_the_env_api_token(
    tmp_kit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-010: an env-only container runs with one token, `ROOMBA_KIT_API_TOKEN`."""
    tokens = TokenStore(CredentialStore())
    assert tokens.verify("rk_env-token-0123456789") is None
    monkeypatch.setenv("ROOMBA_KIT_API_TOKEN", "rk_env-token-0123456789")
    assert tokens.verify("rk_env-token-0123456789") == "env"
    assert tokens.verify("rk_env-token-012345678") is None
    tokens.check()
    monkeypatch.setenv("ROOMBA_KIT_API_TOKEN", "short")
    assert tokens.verify("short") is None  # too short to accept
    with pytest.raises(KitError) as err:
        tokens.check()  # and the service refuses to start with it
    assert "ROOMBA_KIT_API_TOKEN" in err.value.message


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


def test_rewrites_keep_what_the_kit_does_not_model(tmp_kit_home: Path) -> None:
    """Unknown keys and sections, and explicit sections/defaults, survive a kit rewrite."""
    paths.config_file().write_text(
        "[server]\nport = 9000\nworkers = 2\n"
        "[alerts]\ndock_search_minutes = 5\n"  # explicit, equal to the default
        "[history]\n"  # explicit, empty
        '[custom]\nnote = "mine"\nwhen = 2026-11-08T12:00:00Z\n[custom.nested]\nx = [1, 2]\n'
        f'[[robots]]\nid = "{BLID}"\nname = "Kitchen"\ncolour = "red"\n'
    )
    other = "00000000000000000000000000000002"
    upsert_robot(RobotEntry(id=other, name="Mop", address="192.0.2.11"))
    upsert_robot(RobotEntry(id=BLID, name="Hall", address="192.0.2.10"))
    config = load_config()
    config.server.port = 8765  # back to the default, but the key was explicit: keep it
    save_config(config)
    written = tomllib.loads(paths.config_file().read_text())
    assert written["server"] == {"port": 8765, "workers": 2}
    assert written["alerts"] == {"dock_search_minutes": 5}
    assert written["history"] == {}
    assert written["custom"]["note"] == "mine" and written["custom"]["nested"] == {"x": [1, 2]}
    assert written["custom"]["when"].isoformat() == "2026-11-08T12:00:00+00:00"
    robots = written["robots"]
    assert [r["id"] for r in robots] == [BLID, other]
    assert robots[0] == {"id": BLID, "name": "Hall", "address": "192.0.2.10", "colour": "red"}
    assert remove_robot(other)
    assert tomllib.loads(paths.config_file().read_text())["robots"] == [robots[0]]
    assert "cloud" not in written  # a section never written stays absent


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


# Credentials


def test_credentials_created_0600_and_env_layered(
    tmp_kit_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = CredentialStore()
    creds.set_robot_password(BLID, CANARY)
    assert mode(creds.path) == 0o600
    assert creds.robot_password(BLID) == CANARY
    monkeypatch.setenv(f"ROOMBA_KIT_ROBOTS__{BLID}__PASSWORD", "CANARY-env-pw")
    assert creds.robot_password(BLID) == "CANARY-env-pw"
    monkeypatch.setenv("ROOMBA_KIT_CLOUD__USERNAME", "canary@example.invalid")
    monkeypatch.setenv("ROOMBA_KIT_CLOUD__PASSWORD", CLOUD_CANARY)
    cloud = creds.cloud()
    assert cloud is not None and cloud.password == CLOUD_CANARY
    assert CLOUD_CANARY not in repr(cloud)
    creds.set_cloud("file@example.invalid", "CANARY-file-cloud")
    text = creds.path.read_text()
    assert "CANARY-env-pw" not in text and CLOUD_CANARY not in text
    assert {CANARY, "CANARY-env-pw", CLOUD_CANARY} <= set(creds.secrets())


def test_broad_mode_is_refused_with_chmod_hint(tmp_kit_home: Path) -> None:
    creds = CredentialStore()
    creds.set_robot_password(BLID, CANARY)
    os.chmod(creds.path, 0o644)
    with pytest.raises(KitError) as err:
        CredentialStore().robot_password(BLID)
    assert err.value.code is ErrorCode.INVALID_REQUEST
    assert f"chmod 600 {creds.path}" in err.value.message
    assert CANARY not in err.value.message
    with pytest.raises(KitError):  # an already-open store notices the chmod too
        creds.robot_password(BLID)


def test_reload_on_change_from_another_writer(tmp_kit_home: Path) -> None:
    service, cli = CredentialStore(), CredentialStore()
    assert service.robot_password(BLID) is None
    cli.set_robot_password(BLID, CANARY)
    assert service.robot_password(BLID) == CANARY


# Tokens


def test_tokens_are_stored_hashed(tmp_kit_home: Path) -> None:
    tokens = TokenStore(CredentialStore())
    plain = tokens.create("hermes")
    stored = paths.credentials_file().read_text()
    assert plain not in stored and "sha256" in stored
    assert tokens.verify(plain) == "hermes"
    assert tokens.verify(plain + "x") is None
    with pytest.raises(KitError):
        tokens.create("hermes")
    [info] = tokens.list()
    assert info.name == "hermes" and info.last_used_at is not None
    assert set(vars(info)) == {"name", "created_at", "last_used_at"}


def test_cli_token_file_is_0600(tmp_kit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = TokenStore(CredentialStore())
    plain = tokens.create("cli")
    assert mode(paths.cli_token_file()) == 0o600
    assert read_cli_token() == plain
    monkeypatch.setenv("ROOMBA_KIT_API_TOKEN", "CANARY-env-token")
    assert read_cli_token() == "CANARY-env-token"
    assert tokens.create("cli", replace=True) != plain
    assert tokens.verify(plain) is None
    assert tokens.revoke("cli") and not paths.cli_token_file().exists()


def test_broad_cli_token_is_refused(tmp_kit_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-013: the plaintext CLI token gets the same 0600 check as credentials.toml."""
    tokens = TokenStore(CredentialStore())
    plain = tokens.create("cli")
    os.chmod(paths.cli_token_file(), 0o644)
    with pytest.raises(KitError) as err:
        read_cli_token()
    assert err.value.code is ErrorCode.INVALID_REQUEST
    assert f"chmod 600 {paths.cli_token_file()}" in err.value.message
    assert plain in set(tokens.secrets())  # the redactor still masks it
    monkeypatch.setenv("ROOMBA_KIT_API_TOKEN", "CANARY-env-token")
    assert read_cli_token() == "CANARY-env-token"  # env wins; the file isn't read


def test_revoke_in_another_process_takes_effect(tmp_kit_home: Path) -> None:
    service = TokenStore(CredentialStore())
    plain = TokenStore(CredentialStore()).create("phone")
    assert service.verify(plain) == "phone"
    assert TokenStore(CredentialStore()).revoke("phone")
    assert service.verify(plain) is None


def test_last_used_persistence_is_throttled(tmp_kit_home: Path) -> None:
    now = [datetime(2026, 11, 8, 20, 0, tzinfo=UTC)]
    tokens = TokenStore(CredentialStore(), persist_every_s=60, clock=lambda: now[0])
    plain = tokens.create("phone")
    tokens.verify(plain)  # first use persists
    before = paths.credentials_file().stat().st_mtime_ns
    now[0] += timedelta(seconds=10)
    tokens.verify(plain)
    assert paths.credentials_file().stat().st_mtime_ns == before
    assert tokens.list()[0].last_used_at == now[0]
    tokens.flush()
    assert "2026-11-08T20:00:10Z" in paths.credentials_file().read_text()


# Redaction


def test_redact_masks_known_values_and_patterns() -> None:
    secrets = [CANARY]
    f = RedactFilter(lambda: secrets)
    record = logging.LogRecord("t", logging.INFO, __file__, 1, f"pw {CANARY} and %s", ("x",), None)
    f.filter(record)
    assert CANARY not in record.getMessage()
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "connect %s", (CANARY,), None)
    f.filter(record)
    assert CANARY not in record.getMessage() and "***" in record.getMessage()
    secrets.append("CANARY-late")  # picked up without rebuilding the filter
    assert "CANARY-late" not in f.redact("now CANARY-late")
    for line in (
        "password=hunter2-canary",
        'payload {"password": "two words canary"}',
        "Authorization: Bearer abc.def-canary",
        "api_token: tok-canary",
        "minted rk_abcdefghijklmnopqrstuvwxyz",
    ):
        out = f.redact(line)
        assert "canary" not in out and "abcdefghijklmnop" not in out and "***" in out, out


def test_redact_leaves_clean_records_alone_and_masks_tracebacks() -> None:
    f = RedactFilter(lambda: [CANARY])
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "%s %s", ("a", 1), None)
    f.filter(record)
    assert record.args == ("a", 1)
    try:
        raise RuntimeError(f"boom {CANARY}")
    except RuntimeError:
        import sys

        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "x", None, sys.exc_info())
    f.filter(record)
    assert record.exc_text and CANARY not in record.exc_text


def test_install_redaction_covers_every_logger(caplog: pytest.LogCaptureFixture) -> None:
    previous = logging.getLogRecordFactory()
    try:
        install_redaction(lambda: [CANARY])
        with caplog.at_level(logging.INFO):
            logging.getLogger("roomba_kit.some.module").info("robot pw %s", CANARY)
        assert caplog.records and CANARY not in caplog.text
    finally:
        logging.setLogRecordFactory(previous)
