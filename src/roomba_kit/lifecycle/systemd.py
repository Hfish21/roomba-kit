"""Linux start-on-boot: a `systemd --user` unit (research R11, T090)."""

import getpass
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from roomba_kit.config.paths import atomic_write
from roomba_kit.lifecycle import install
from roomba_kit.lifecycle.install import Runner, ServiceSpec

UNIT = "roomba-kit.service"
TEMPLATE = "roomba-kit.service.tmpl"
_BARE = re.compile(r"[A-Za-z0-9_@%+=:,./$-]+")


def unit_path(home: Path | None = None) -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else (home or Path.home()) / ".config"
    return root / "systemd" / "user" / UNIT


def _specifiers(value: str) -> str:
    return value.replace("%", "%%")


def _quoted(value: str) -> str:
    return '"' + _specifiers(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _exec_arg(arg: str) -> str:
    """One ExecStart word: `%` and `$` are expanded there, so both are doubled."""
    arg = arg.replace("$", "$$")
    return _specifiers(arg) if _BARE.fullmatch(arg) else _quoted(arg)


def render(spec: ServiceSpec) -> str:
    install.refuse_control_chars(spec)
    return install.render_template(
        TEMPLATE,
        {
            "exec_start": " ".join(_exec_arg(arg) for arg in spec.command),
            "environment": "\n".join(
                f"Environment={_quoted(f'{k}={v}')}" for k, v in spec.env.items()
            ),
            "log": _specifiers(str(spec.log_path)),
        },
    )


@dataclass
class Systemd:
    path: Path = field(default_factory=unit_path)
    run: Runner = install.run_command
    user: str = field(default_factory=getpass.getuser)
    name: str = "systemd"

    def render(self, spec: ServiceSpec) -> str:
        return render(spec)

    def _ctl(self, *args: str, checked: bool = True) -> install.Result:
        argv = ["systemctl", "--user", *args]
        result = self.run(argv)
        return install.check(result, argv) if checked else result

    def install(self, spec: ServiceSpec) -> list[str]:
        """Write the unit, enable and start it; restart it if it was already running."""
        text = render(spec)
        install.prepare_log(spec.log_path)
        active = self._ctl("is-active", "--quiet", UNIT, checked=False).returncode == 0
        atomic_write(self.path, text, 0o644)
        self._ctl("daemon-reload")
        self._ctl("enable", "--now", UNIT)
        if active:
            self._ctl("restart", UNIT)
        return [f"To start at boot without logging in, run: loginctl enable-linger {self.user}"]

    def uninstall(self) -> bool:
        """Stop, disable and delete the unit. False when nothing was installed."""
        if not self.path.exists():
            return False
        self._ctl("disable", "--now", UNIT)
        self.path.unlink()
        self._ctl("daemon-reload")
        return True
