"""Rendered start-on-boot units. Rendering only: nothing is installed or run."""

import plistlib
from importlib.resources import files
from pathlib import Path

import pytest

from roomba_kit.lifecycle import install, launchd, systemd

SPEC = install.ServiceSpec(
    command=("/opt/kit/bin/python", "-m", "roomba_kit.cli.main", "serve"),
    env={"ROOMBA_KIT_CONFIG_DIR": "/home/u/.config/roomba-kit", "ROOMBA_KIT_STATE_DIR": "/st"},
    log_path=Path("/st/service.log"),
)


def test_render_substitutes_strictly():
    assert install.render_text("a=$a b=$$b", {"a": "1"}) == "a=1 b=$b"
    with pytest.raises(KeyError):
        install.render_text("$a $b", {"a": "1"})
    with pytest.raises(ValueError, match="unused"):
        install.render_text("$a", {"a": "1", "extra": "2"})


def test_templates_ship_inside_the_package():
    templates = files("roomba_kit.lifecycle").joinpath("templates")
    for name in (launchd.TEMPLATE, systemd.TEMPLATE):
        assert templates.joinpath(name).is_file()


def test_plist():
    plist = plistlib.loads(launchd.render(SPEC).encode())
    assert plist == {
        "Label": "com.roomba-kit.service",
        "ProgramArguments": list(SPEC.command),
        "EnvironmentVariables": dict(SPEC.env),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": "/st/service.log",
        "StandardErrorPath": "/st/service.log",
    }


def test_plist_escapes_xml():
    spec = install.ServiceSpec(
        command=("/Users/a&b/<py>", "serve"), env={"K": "x&y"}, log_path=Path("/l&g")
    )
    plist = plistlib.loads(launchd.render(spec).encode())
    assert plist["ProgramArguments"] == ["/Users/a&b/<py>", "serve"]
    assert plist["EnvironmentVariables"] == {"K": "x&y"}
    assert plist["StandardOutPath"] == "/l&g"


def test_plist_path():
    home = Path("/home/u")
    assert launchd.plist_path(home) == home / "Library/LaunchAgents/com.roomba-kit.service.plist"


def lines(text):
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def test_systemd_unit():
    unit = lines(systemd.render(SPEC))
    assert "ExecStart=/opt/kit/bin/python -m roomba_kit.cli.main serve" in unit
    assert 'Environment="ROOMBA_KIT_CONFIG_DIR=/home/u/.config/roomba-kit"' in unit
    assert 'Environment="ROOMBA_KIT_STATE_DIR=/st"' in unit
    assert "Restart=on-failure" in unit
    assert "StandardOutput=append:/st/service.log" in unit
    assert "StandardError=append:/st/service.log" in unit
    assert unit[unit.index("[Install]") + 1] == "WantedBy=default.target"


def test_systemd_escapes_specifiers_quotes_and_dollars():
    spec = install.ServiceSpec(
        command=('/home/my user/py "x"', "100%", "$HOME"),
        env={"K": 'a "b" 5%'},
        log_path=Path("/st/50%.log"),
    )
    unit = lines(systemd.render(spec))
    assert r'ExecStart="/home/my user/py \"x\"" 100%% $$HOME' in unit
    assert r'Environment="K=a \"b\" 5%%"' in unit
    assert "StandardOutput=append:/st/50%%.log" in unit


@pytest.mark.parametrize("renderer", [launchd.render, systemd.render])
def test_control_characters_are_refused(renderer):
    spec = install.ServiceSpec(command=("/py\nExecStartPre=/evil",), env={}, log_path=Path("/l"))
    with pytest.raises(install.InstallError, match="control character"):
        renderer(spec)


def test_systemd_unit_path(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert systemd.unit_path(Path("/home/u")) == Path(
        "/home/u/.config/systemd/user/roomba-kit.service"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    assert systemd.unit_path(Path("/home/u")) == Path("/xdg/systemd/user/roomba-kit.service")
