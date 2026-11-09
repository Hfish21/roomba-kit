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


def _reconnected(v: ConfirmView) -> bool:
    return v.reconnected_since_send


DEFAULT_SPECS: dict[str, CommandSpec] = {
    "clean": CommandSpec(_activity_is(Activity.CLEANING), 20),
    "clean_rooms": CommandSpec(_activity_is(Activity.CLEANING), 20),
    "pause": CommandSpec(_activity_is(Activity.PAUSED), 10),
    "resume": CommandSpec(_activity_is(Activity.CLEANING), 10),
    "stop": CommandSpec(_stopped, 10),
    # A docked robot below 100% reports `charging`, not `docked`.
    "dock": CommandSpec(_activity_is(Activity.RETURNING, Activity.DOCKED, Activity.CHARGING), 10),
    "find": CommandSpec(_find_echoed, 5),
    "evac": CommandSpec(_activity_is(Activity.EMPTYING_BIN), 15),
    "reboot": CommandSpec(_reconnected, 180),
    "train": CommandSpec(_activity_is(Activity.MAPPING), 20),
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
