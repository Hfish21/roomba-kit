"""Capture a robot's reported state as a scrubbed profile fixture (contracts/model-profile.md).

    uv run python scripts/capture_fixture.py ROBOT --profile <new_profile_id>
    uv run python scripts/capture_fixture.py ROBOT --out /tmp/state.json --seconds 60

A developer tool, not a kit surface: raw payloads never reach the API, MCP or CLI. It
connects to the robot directly, so the service must be stopped first (robots accept one local
connection). ROBOT is a configured robot's ID or name; its address
and password come from the kit's config and credentials. The default output is
`tests/fixtures/profiles/<profile>/state.json`.
"""

import argparse
import asyncio
import copy
import ipaddress
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from roomba_kit.cli.client import local_service_url
from roomba_kit.config import paths
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import RobotEntry, load_settings
from roomba_kit.core.errors import KitError
from roomba_kit.lifecycle import daemon
from roomba_kit.service.normalize import merge
from roomba_kit.transport.local import LocalTransport

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "profiles"
DEFAULT_SECONDS = 30.0
ROBOT_PLACEHOLDER = "0" * 31 + "1"
NAME_PLACEHOLDER = "Robot"
EMAIL_PLACEHOLDER = "user@example.com"

# tests/fixtures/captures/README.md, "Removed keys": exact names, and substrings (any case).
REMOVED_KEYS = frozenset(
    {
        "passwordHash", "netinfo", "wlcfg", "svcEndpoints", "batInfo", "hwDbgr", "mac",
        "wlan0HwAddr", "navSerialNo", "mobBlid", "cloudEnv", "country", "timezone", "tz",
        "langs2",
    }
)  # fmt: skip
REMOVED_SUBSTRINGS = ("password", "ssid", "serial", "token", "email", "username")

_HEX_ID = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32}(?![0-9A-Fa-f])")
# Bounded so firmware strings like `linux+4.0.4.5+…` or `3.13.0.6-tags` aren't addresses.
_IPV4 = re.compile(r"(?<![\w.+-])(?:\d{1,3}\.){3}\d{1,3}(?![\w.+-])")
_MAC = re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _removed(key: str) -> bool:
    lowered = key.lower()
    return key in REMOVED_KEYS or any(s in lowered for s in REMOVED_SUBSTRINGS)


class _Scrubber:
    """Stable placeholders, numbered in document order, for one payload."""

    def __init__(self, blid: str, robot_id: str) -> None:
        self.blid = re.compile(re.escape(blid), re.IGNORECASE) if blid else None
        self.robot_id = robot_id
        self.ids: dict[str, str] = {}
        self.ips: dict[str, str] = {}
        self.macs: dict[str, str] = {}

    def text(self, value: str) -> str:
        if self.blid is not None:
            value = self.blid.sub(self.robot_id, value)
        value = _HEX_ID.sub(self._hex_id, value)
        value = _IPV4.sub(self._ip, value)
        value = _MAC.sub(self._mac, value)
        return _EMAIL.sub(EMAIL_PLACEHOLDER, value)

    def _hex_id(self, match: re.Match[str]) -> str:
        found = match.group(0).upper()
        if found.startswith("0" * 24) or found.startswith("F" * 24):
            return match.group(0)  # already a placeholder
        return self.ids.setdefault(found, "F" * 28 + f"{len(self.ids) + 1:04d}")

    def _ip(self, match: re.Match[str]) -> str:
        found = match.group(0)
        try:
            address = ipaddress.IPv4Address(found)
        except ValueError:
            return found  # not an address (an octet over 255)
        if address in ipaddress.IPv4Network("192.0.2.0/24") or address.is_unspecified:
            return found
        if found == "255.255.255.0" or address.is_loopback:
            return found  # a netmask or loopback identifies nothing
        return self.ips.setdefault(found, f"192.0.2.{len(self.ips) + 1}")

    def _mac(self, match: re.Match[str]) -> str:
        found = match.group(0).lower().replace("-", ":")
        if found.startswith("00:00:5e:00:53:"):
            return found  # RFC 7042 documentation range
        return self.macs.setdefault(found, f"00:00:5e:00:53:{len(self.macs) + 1:02x}")

    def walk(self, value: Any) -> Any:
        if isinstance(value, Mapping):
            return {k: self.walk(v) for k, v in value.items() if not _removed(str(k))}
        if isinstance(value, list):
            return [self.walk(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value


def scrub(
    reported: Mapping[str, Any],
    *,
    blid: str,
    robot_id: str = ROBOT_PLACEHOLDER,
    name: str = NAME_PLACEHOLDER,
) -> dict[str, Any]:
    """A copy of a merged `state.reported` that's safe to commit. The input is not modified.

    Removes credential and network keys at any depth; replaces the robot's BLID with
    `robot_id`, other 32-hex IDs with `FFFF…000N`, IPs with `192.0.2.N`, MACs with
    `00:00:5e:00:53:NN`, emails with `user@example.com`, and the display name with `name`.
    """
    out: dict[str, Any] = _Scrubber(blid, robot_id).walk(copy.deepcopy(dict(reported)))
    if "name" in out:
        out["name"] = name
    return out


def refuse_if_service_running(pid_file: Path, url: str) -> None:
    """Exit if the kit's service could be holding a robot connection."""
    pid = daemon.read_pid(pid_file)
    if pid is not None and daemon.alive(pid):
        sys.exit(f"The service is running (pid {pid}). Stop the service first: "
                 "`roomba-kit service stop`.")  # fmt: skip
    if daemon.health(url, timeout=0.5) is not None:
        sys.exit(f"A service is answering at {url}. Stop the service first.")


def _label(entry: RobotEntry) -> str:
    """How to name the robot on the terminal: never by its BLID, which is an identifier."""
    return repr(entry.name) if entry.name else "the robot"


def find_robot(robots: Sequence[RobotEntry], ref: str) -> RobotEntry:
    """A configured robot by ID, or by an unambiguous name (case-insensitive)."""
    for entry in robots:
        if entry.id.lower() == ref.lower():
            return entry
    named = [r for r in robots if r.name.casefold() == ref.casefold()]
    if len(named) > 1:
        sys.exit(f"More than one robot is named {ref!r}; use its ID.")
    if not named:
        sys.exit(f"No robot {ref!r} in the kit's config. Add it with `roomba-kit robots add`.")
    return named[0]


async def collect(entry: RobotEntry, password: str, seconds: float) -> dict[str, Any]:
    """Connect once, merge every reported fragment for `seconds`, then disconnect."""
    if not entry.address:
        sys.exit(f"{_label(entry)} has no address; rediscover it with the kit first.")
    transport = LocalTransport(entry.id, entry.address, password)
    reported: dict[str, Any] = {}
    dropped: list[str | None] = []

    def on_reported(fragment: dict[str, Any]) -> None:
        nonlocal reported
        reported = merge(reported, fragment)

    def on_connection(connected: bool, reason: str | None) -> None:
        if not connected:
            dropped.append(reason)

    transport.on_reported(on_reported)
    transport.on_connection(on_connection)
    try:
        await transport.connect()
    except KitError as err:
        sys.exit(f"Could not connect: {err.code.value}: {err.message}")
    try:
        await asyncio.sleep(seconds)
    finally:
        await transport.disconnect()
    if dropped:
        sys.exit(f"The connection dropped during capture ({dropped[0]}); nothing was written.")
    if not reported:
        sys.exit("The robot reported no state; nothing was written.")
    return reported


def output_path(args: argparse.Namespace, entry: RobotEntry) -> Path:
    if args.out is not None:
        return Path(args.out)
    profile = args.profile or entry.profile
    if not profile:
        sys.exit("This robot has no profile yet: pass --profile <new_profile_id> or --out.")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", profile):
        sys.exit(f"Profile ID {profile!r} must be snake_case.")
    return FIXTURE_DIR / profile / "state.json"


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("robot", help="Configured robot ID or name")
    parser.add_argument("--profile", help="Profile ID for the default output path")
    parser.add_argument("--out", help="Write here instead of tests/fixtures/profiles/<id>/")
    parser.add_argument(
        "--seconds", type=float, default=DEFAULT_SECONDS, help="How long to collect state"
    )
    parser.add_argument(
        "--placeholder-id",
        default=ROBOT_PLACEHOLDER,
        help="Placeholder that replaces the robot's BLID",
    )
    parser.add_argument("--name", default=NAME_PLACEHOLDER, help="Placeholder display name")
    args = parser.parse_args(argv)
    if args.seconds <= 0:
        parser.error("--seconds must be positive")

    refuse_if_service_running(paths.pid_file(), local_service_url())
    entry = find_robot(load_settings().robots, args.robot)
    out = output_path(args, entry)
    password = CredentialStore().robot_password(entry.id)
    if password is None:
        sys.exit(f"No password stored for {_label(entry)}. Run `roomba-kit robots password`.")

    print(f"Collecting {args.seconds:g} s of state from {_label(entry)}…", file=sys.stderr)
    reported = asyncio.run(collect(entry, password, args.seconds))
    clean = scrub(reported, blid=entry.id, robot_id=args.placeholder_id, name=args.name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(clean, indent=1) + "\n")
    print(f"Wrote {len(clean)} keys (sku {clean.get('sku', '?')}) to {out}.", file=sys.stderr)
    print("Review the file before committing it.", file=sys.stderr)


if __name__ == "__main__":
    main()
