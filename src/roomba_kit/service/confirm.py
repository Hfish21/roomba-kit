"""Command confirmation (research R9): did the robot's state show the command took effect?"""

import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from roomba_kit.core.models import Activity, CommandResult, RobotState, StateSummary
from roomba_kit.profiles.base import CommandSpec, ConfirmView, ModelProfile


class ConfirmSource(Protocol):
    """What `await_confirmation` watches. `RobotSession` provides it; tests fake it."""

    @property
    def state(self) -> RobotState:
        """The current normalized state (the last known one while disconnected)."""
        ...

    @property
    def change_seq(self) -> int:
        """Increments on every state update and every connection change."""
        ...

    def reconnected_since(self, at: datetime) -> bool:
        """True when the link dropped and came back up after `at`."""
        ...

    async def wait_change(self, after_seq: int, timeout_s: float) -> None:
        """Return once `change_seq > after_seq`, or after `timeout_s`. Never raises on timeout."""
        ...


def _activity_is(*wanted: Activity) -> Callable[[ConfirmView], bool]:
    return lambda v: v.now.activity in wanted


def _stopped(v: ConfirmView) -> bool:
    return v.now.mission is None


def _find_echoed(v: ConfirmView) -> bool:
    now = v.now.last_command
    if now is None or now.command != "find" or now.at is None:
        return False
    prior = v.before.last_command if v.before else None
    # Echo times are whole seconds, so a `find` right after another command can share its
    # timestamp. Only a prior `find` echo needs a strictly newer time to count as fresh.
    if prior is None or prior.command != "find" or prior.at is None:
        return True
    return now.at > prior.at


def _fresh_echo(v: ConfirmView, names: frozenset[str]) -> bool:
    """The robot echoed one of `names` after `before` (HW-1: real robots echo every command)."""
    now = v.now.last_command
    if now is None or now.command not in names:
        return False
    if v.echoed_since_send is not None:
        return v.echoed_since_send  # a real echo message arrived after the send
    prior = v.before.last_command if v.before else None
    if prior is None or prior.command not in names:
        return True
    return now.at is not None and prior.at is not None and now.at > prior.at


def _mission_changed(v: ConfirmView) -> bool:
    before = v.before.mission if v.before else None
    now = v.now.mission
    return now is not None and (before is None or now.id != before.id)


def _started(activity: Activity, *echoes: str) -> Callable[[ConfirmView], bool]:
    """`activity` reached; if the robot was already in it, only with a fresh effect (FR-029).

    Without that, `clean` sent to a cleaning robot that ignored it would confirm at 0 ms.
    """
    names = frozenset(echoes)

    def predicate(v: ConfirmView) -> bool:
        if v.now.activity is not activity:
            return False
        if v.before is None or v.before.activity is not activity:
            return True
        return _fresh_echo(v, names) or _mission_changed(v)

    return predicate


def _reconnected(v: ConfirmView) -> bool:
    return v.reconnected_since_send


DEFAULT_SPECS: dict[str, CommandSpec] = {
    # Both are `start` on the wire; an echo normalizes to either, depending on its regions.
    "clean": CommandSpec(_started(Activity.CLEANING, "clean", "clean_rooms"), 20),
    "clean_rooms": CommandSpec(_started(Activity.CLEANING, "clean", "clean_rooms"), 20),
    "pause": CommandSpec(_activity_is(Activity.PAUSED), 10),
    "resume": CommandSpec(_activity_is(Activity.CLEANING), 10),
    "stop": CommandSpec(_stopped, 10),
    # A docked robot below 100% reports `charging`, not `docked`.
    "dock": CommandSpec(_activity_is(Activity.RETURNING, Activity.DOCKED, Activity.CHARGING), 10),
    "find": CommandSpec(_find_echoed, 5),
    "evac": CommandSpec(_activity_is(Activity.EMPTYING_BIN), 15),
    "reboot": CommandSpec(_reconnected, 180),
    "train": CommandSpec(_started(Activity.MAPPING, "train"), 20),
}


def spec_for(command: str, profile: ModelProfile) -> CommandSpec:
    """The profile's override for `command`, else the R9 default. KeyError for unknown commands."""
    if (spec := profile.command_overrides.get(command)) is not None:
        return spec
    default = DEFAULT_SPECS[command]
    window = profile.command_windows.get(command)
    return default if window is None else CommandSpec(default.predicate, window)


async def await_confirmation(
    source: ConfirmSource,
    command: str,
    spec: CommandSpec,
    *,
    before: RobotState | None,
    sent_at: datetime,
    echoes_before: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CommandResult:
    """Wait up to `spec.window_s` for `spec.predicate`, re-checking on every change.

    Call right after sending, with `before` captured just before the send.
    """
    start = clock()
    deadline = start + spec.window_s
    while True:
        seq = source.change_seq
        now = source.state
        view = ConfirmView(
            sent_at=sent_at,
            before=before,
            now=now,
            reconnected_since_send=source.reconnected_since(sent_at),
            echoed_since_send=(
                None
                if echoes_before is None
                else getattr(source, "echo_count", echoes_before) > echoes_before
            ),
        )
        confirmed = spec.predicate(view)
        remaining = deadline - clock()
        if confirmed or remaining <= 0:
            return CommandResult(
                robot_id=now.robot_id,
                command=command,
                outcome="confirmed" if confirmed else "unconfirmed",
                state=StateSummary(activity=now.activity, mission=now.mission),
                waited_ms=max(0, round((clock() - start) * 1000)),
            )
        await source.wait_change(seq, remaining)
