"""`service install|uninstall` (T089, T090, T092) against a fake supervisor.

Nothing here runs `launchctl`/`systemctl` or writes outside `tmp_path`.
"""

import json
import plistlib
import stat
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from roomba_kit.cli import lifecycle, main
from roomba_kit.config import paths
from roomba_kit.core.errors import CLI_EXIT, ErrorCode
from roomba_kit.lifecycle import daemon, install, launchd, systemd


@pytest.fixture(autouse=True)
def no_real_commands(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"tried to run a real command: {args}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


class Runner:
    """Records commands; answers each by the first matching prefix in `codes`."""

    def __init__(self, codes=None):
        self.codes = codes or {}
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        for prefix, results in self.codes.items():
            if tuple(args[: len(prefix)]) == prefix:
                code = results.pop(0) if isinstance(results, list) else results
                return install.Result(code, f"{args[1]} said no" if code else "")
        return install.Result(0, "")


def spec(tmp_path):
    return install.ServiceSpec(
        command=("/py", "-m", "roomba_kit.cli.main", "serve"),
        env={"ROOMBA_KIT_STATE_DIR": str(tmp_path / "state")},
        log_path=tmp_path / "state" / "service.log",
    )


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


PRINT = ("launchctl", "print")
TARGET = "gui/501/com.roomba-kit.service"


def make_launchd(tmp_path, runner):
    path = tmp_path / "LaunchAgents" / "com.roomba-kit.service.plist"
    return launchd.Launchd(path=path, uid=501, run=runner, sleep=lambda s: None)


def test_launchd_install_fresh(tmp_path):
    runner = Runner({PRINT: 113})
    manager = make_launchd(tmp_path, runner)
    assert manager.install(spec(tmp_path)) == []
    assert runner.calls == [
        ["launchctl", "print", TARGET],
        ["launchctl", "bootstrap", "gui/501", str(manager.path)],
    ]
    assert manager.path.read_text() == launchd.render(spec(tmp_path))
    assert mode(manager.path) == 0o644
    assert mode(spec(tmp_path).log_path) == 0o600


def test_launchd_reinstall_boots_out_first_and_retries_bootstrap(tmp_path):
    runner = Runner({PRINT: 0, ("launchctl", "bootstrap"): [5, 0]})
    manager = make_launchd(tmp_path, runner)
    manager.install(spec(tmp_path))
    assert [c[1] for c in runner.calls] == ["print", "bootout", "bootstrap", "bootstrap"]
    assert runner.calls[1] == ["launchctl", "bootout", TARGET]


def test_launchd_bootstrap_failure_is_reported(tmp_path):
    runner = Runner({PRINT: 113, ("launchctl", "bootstrap"): 5})
    with pytest.raises(install.InstallError, match="bootstrap said no"):
        make_launchd(tmp_path, runner).install(spec(tmp_path))


def test_launchd_uninstall(tmp_path):
    runner = Runner({PRINT: 0})
    manager = make_launchd(tmp_path, runner)
    manager.path.parent.mkdir()
    manager.path.write_text("x")
    assert manager.uninstall() is True
    assert runner.calls[1] == ["launchctl", "bootout", TARGET]
    assert not manager.path.exists()

    runner = Runner({PRINT: 113})
    assert make_launchd(tmp_path, runner).uninstall() is False
    assert runner.calls == [["launchctl", "print", TARGET]]


def ctl(*args):
    return ["systemctl", "--user", *args]


IS_ACTIVE = ("systemctl", "--user", "is-active")


def make_systemd(tmp_path, runner):
    path = tmp_path / "systemd" / "user" / "roomba-kit.service"
    return systemd.Systemd(path=path, run=runner, user="kit")


def test_systemd_install_fresh(tmp_path):
    runner = Runner({IS_ACTIVE: 3})
    manager = make_systemd(tmp_path, runner)
    notes = manager.install(spec(tmp_path))
    assert runner.calls == [
        ctl("is-active", "--quiet", "roomba-kit.service"),
        ctl("daemon-reload"),
        ctl("enable", "--now", "roomba-kit.service"),
    ]
    assert manager.path.read_text() == systemd.render(spec(tmp_path))
    assert notes == ["To start at boot without logging in, run: loginctl enable-linger kit"]


def test_systemd_reinstall_restarts(tmp_path):
    runner = Runner({IS_ACTIVE: 0})
    make_systemd(tmp_path, runner).install(spec(tmp_path))
    assert runner.calls[-1] == ctl("restart", "roomba-kit.service")


def test_systemd_failure_is_reported(tmp_path):
    runner = Runner({IS_ACTIVE: 3, ("systemctl", "--user", "enable"): 1})
    with pytest.raises(install.InstallError, match="said no"):
        make_systemd(tmp_path, runner).install(spec(tmp_path))


def test_systemd_uninstall(tmp_path):
    runner = Runner()
    manager = make_systemd(tmp_path, runner)
    assert manager.uninstall() is False and runner.calls == []
    manager.path.parent.mkdir(parents=True)
    manager.path.write_text("x")
    assert manager.uninstall() is True
    assert runner.calls == [ctl("disable", "--now", "roomba-kit.service"), ctl("daemon-reload")]
    assert not manager.path.exists()


def test_missing_supervisor_is_not_supported(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("launchctl")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(install.InstallError, match="launchctl not found") as exc:
        install.run_command(["launchctl", "print"])
    assert exc.value.code is ErrorCode.NOT_SUPPORTED


@pytest.fixture
def fake_launchd(tmp_kit_home, monkeypatch):
    runner = Runner({PRINT: 113})
    manager = make_launchd(tmp_kit_home, runner)
    monkeypatch.setattr(lifecycle, "service_manager", lambda: manager)
    monkeypatch.setattr(daemon, "health", lambda url, timeout=1.0: {"status": "ok"})
    return manager


def cli(*args):
    return CliRunner().invoke(main.cli, list(args))


def test_cli_install_print_renders_without_installing(fake_launchd):
    result = cli("service", "install", "--print")
    assert result.exit_code == 0, result.output
    plist = plistlib.loads(result.stdout.encode())
    assert plist["ProgramArguments"] == lifecycle.serve_command()
    assert plist["EnvironmentVariables"] == {
        "ROOMBA_KIT_CONFIG_DIR": str(paths.config_dir()),
        "ROOMBA_KIT_STATE_DIR": str(paths.state_dir()),
    }
    assert fake_launchd.run.calls == [] and not fake_launchd.path.exists()


def test_cli_install_and_uninstall(fake_launchd):
    result = cli("--json", "service", "install")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "supervisor": "launchd",
        "path": str(fake_launchd.path),
        "installed": True,
        "healthy": True,
        "notes": [],
    }
    assert fake_launchd.path.exists()

    result = cli("--json", "service", "uninstall")
    assert json.loads(result.stdout) == {
        "supervisor": "launchd",
        "path": str(fake_launchd.path),
        "removed": True,
    }
    assert "Not installed" in cli("service", "uninstall").stdout


def test_cli_install_unhealthy_exits_69(fake_launchd, monkeypatch):
    monkeypatch.setattr(daemon, "health", lambda url, timeout=1.0: None)
    paths.log_file().write_text("boom\n")
    result = cli("service", "install", "--timeout", "0.2")
    assert result.exit_code == CLI_EXIT[ErrorCode.SERVICE_UNAVAILABLE]
    assert "boom" in result.output and fake_launchd.path.exists()


def test_cli_install_refuses_while_service_start_daemon_runs(fake_launchd, monkeypatch):
    monkeypatch.setattr(daemon, "running_pid", lambda path: 4242)
    result = cli("service", "install")
    assert result.exit_code == CLI_EXIT[ErrorCode.INVALID_REQUEST]
    assert "roomba-kit service stop" in result.output
    assert fake_launchd.run.calls == []


def test_cli_supervisor_failure(fake_launchd):
    fake_launchd.run.codes[("launchctl", "bootstrap")] = 5
    result = cli("service", "install")
    assert result.exit_code == CLI_EXIT[ErrorCode.INTERNAL]
    assert "bootstrap said no" in result.output


@pytest.mark.parametrize(
    ("platform", "kind"), [("darwin", launchd.Launchd), ("linux", systemd.Systemd)]
)
def test_dispatch_by_platform(monkeypatch, platform, kind):
    monkeypatch.setattr(lifecycle.sys, "platform", platform)
    assert isinstance(lifecycle.service_manager(), kind)


def test_other_platforms_are_refused(tmp_kit_home, monkeypatch):
    monkeypatch.setattr(lifecycle.sys, "platform", "win32")
    result = cli("service", "install")
    assert result.exit_code == CLI_EXIT[ErrorCode.NOT_SUPPORTED]
    assert "win32" in result.output and "deployment" in result.output


def test_default_paths_are_under_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert launchd.Launchd().path == launchd.plist_path(tmp_path)
    assert systemd.Systemd().path == systemd.unit_path(tmp_path)
