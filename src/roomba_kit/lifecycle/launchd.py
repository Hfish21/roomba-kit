"""macOS start-at-login: a launchd agent in the user's GUI domain."""

import os
import plistlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from roomba_kit.config.paths import atomic_write
from roomba_kit.lifecycle import install
from roomba_kit.lifecycle.install import InstallError, Runner, ServiceSpec

LABEL = "com.roomba-kit.service"
TEMPLATE = "launchd.plist.tmpl"
BOOTSTRAP_ATTEMPTS = 3


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def render(spec: ServiceSpec) -> str:
    install.refuse_control_chars(spec)
    text = install.render_template(
        TEMPLATE,
        {
            "label": LABEL,
            "program_arguments": "\n".join(
                f"    <string>{escape(arg)}</string>" for arg in spec.command
            ),
            "environment": "\n".join(
                f"    <key>{escape(k)}</key>\n    <string>{escape(v)}</string>"
                for k, v in spec.env.items()
            ),
            "log": escape(str(spec.log_path)),
        },
    )
    plistlib.loads(text.encode())  # A broken template edit fails here, not inside launchd.
    return text


@dataclass
class Launchd:
    path: Path = field(default_factory=plist_path)
    uid: int = field(default_factory=os.getuid)
    run: Runner = install.run_command
    sleep: Callable[[float], None] = time.sleep
    name: str = "launchd"

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    def render(self, spec: ServiceSpec) -> str:
        return render(spec)

    def _launchctl(self, *args: str, checked: bool = True) -> install.Result:
        argv = ["launchctl", *args]
        result = self.run(argv)
        return install.check(result, argv) if checked else result

    def loaded(self) -> bool:
        return self._launchctl("print", f"{self.domain}/{LABEL}", checked=False).returncode == 0

    def install(self, spec: ServiceSpec) -> list[str]:
        """Write the plist and (re)load it. Reinstalling boots out the old agent first."""
        text = render(spec)
        install.prepare_log(spec.log_path)
        if self.loaded():
            self._launchctl("bootout", f"{self.domain}/{LABEL}")
        atomic_write(self.path, text, 0o644)
        # A bootstrap right after a bootout can fail with EIO while launchd finishes the teardown.
        for attempt in range(BOOTSTRAP_ATTEMPTS):
            result = self._launchctl("bootstrap", self.domain, str(self.path), checked=False)
            if result.returncode == 0:
                return []
            if attempt + 1 < BOOTSTRAP_ATTEMPTS:
                self.sleep(1.0)
        raise InstallError(f"`launchctl bootstrap` failed ({result.returncode}): {result.output}")

    def uninstall(self) -> bool:
        """Unload and delete the agent. False when nothing was installed."""
        loaded = self.loaded()
        if loaded:
            self._launchctl("bootout", f"{self.domain}/{LABEL}")
        existed = self.path.exists()
        self.path.unlink(missing_ok=True)
        return loaded or existed
