"""Hardware verification (research R16, Constitution VIII): scripted, reversible checks per
capability, recorded in `verification_runs` and rendered as a markdown report.

- Settings: write an alternate value, read it back, restore the original.
- `find` is the one command checked without `allow_motion`: it only plays a sound.
- Every other command (motion, `evac`, `reboot`) runs only with `allow_motion`.
- `train` runs only when named explicitly *and* with `allow_motion`: it can re-create the map
  and change room IDs.
- Only capabilities the profile declares are ever checked; naming another is `not_supported`.
"""

import asyncio
import copy
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

from pydantic import Field

from roomba_kit import __version__
from roomba_kit.core.capabilities import Capability as C
from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import Activity, CleanBaseState, KitModel
from roomba_kit.profiles.base import ModelProfile
from roomba_kit.service import settings
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.session import RobotSession
from roomba_kit.service.store import format_ts

Result = Literal["pass", "fail", "skipped"]

POSITION_TIMEOUT_S = 5.0
SETTLE_S = 180.0
NOT_READY_WAIT_S = 120.0
"""How long to wait for a not-ready condition to clear before cleaning. HW-1: an i5 reports
not-ready 68 for ~1.5 min after a bin evacuation and refuses to start."""
CLEAN_HOLD_S = 30.0
"""`clean` only passes if the robot is still cleaning this long after confirmation, and the
next step waits for it. HW-1: an i5 reported `cleaning` for 1 s, then aborted; and a `pause`
sent 9 s into a clean (still leaving the dock) ended the mission instead of pausing it,
while one sent 30 s in paused normally."""
STOP_TO_DOCK_S = 5.0
"""Pause between `stop` and the closing `dock`. HW-1: an i5 ignored a dock sent ~3 s after
stop, and accepted one sent later."""
"""How long to wait for the robot to be back on its dock after evac or the motion sequence."""
SEQUENCE = (C.CLEAN, C.PAUSE, C.RESUME, C.STOP, C.DOCK)
"""Motion checks run in this order from the dock, and always end with a dock."""
_DOCKED = {Activity.DOCKED, Activity.CHARGING}
_BACKGROUND: set[asyncio.Task[Any]] = set()
"""Closing docks that outlive a cancelled verify, kept referenced until they finish."""
_STATE_FIELDS = {
    C.STATE_BIN: "bin",
    C.STATE_CLEAN_BASE: "clean_base",
    C.STATE_PAD: "pad",
    C.STATE_TANK: "tank",
    C.STATE_LID: "lid_open",
}
_ORDER = [
    *(c for c in C if c.value.startswith("setting.")),
    C.FIND,
    *_STATE_FIELDS,
    C.LIVE_POSITION,
    *(c for c in C if c.value.startswith("cloud.")),
    C.EVAC,
    *SEQUENCE,
    C.CLEAN_ROOMS,
    C.REBOOT,
    C.TRAIN,
]
"""Check order: nothing that moves the robot runs before every non-motion check is done."""


class CheckResult(KitModel):
    capability: str
    result: Result
    evidence: str


class VerificationRun(KitModel):
    id: int
    robot_id: str
    profile_id: str
    anchor: str = Field(
        description="Stable anchor of this run in `docs/verification/<profile_id>.md`. A "
        "profile cites it as `verified(run='docs/verification/<profile_id>.md#<anchor>')`"
    )
    started_at: datetime
    ended_at: datetime
    allow_motion: bool
    results: list[CheckResult]
    report: str = Field(description="The run as markdown, for `docs/verification/<profile_id>.md`")


def plan(profile: ModelProfile, requested: list[str] | None) -> tuple[list[C], set[C]]:
    """(capabilities to check in run order, the ones named explicitly)."""
    if requested is None:
        return sorted(profile.capabilities, key=_ORDER.index), set()
    if not requested:
        raise KitError(
            ErrorCode.INVALID_REQUEST,
            "Name at least one capability, or omit `capabilities` to check every one.",
        )
    named: list[C] = []
    for name in dict.fromkeys(requested):
        try:
            named.append(C(name))
        except ValueError:
            raise KitError(
                ErrorCode.INVALID_REQUEST,
                f"Unknown capability: {name}",
                details={"valid": sorted(c.value for c in profile.capabilities)},
            ) from None
    if missing := [c.value for c in named if not profile.supports(c)]:
        raise KitError(
            ErrorCode.NOT_SUPPORTED,
            f"{profile.display_name} does not declare {', '.join(missing)}, so it isn't checked.",
            details={"profile": profile.id, "capability": missing[0], "capabilities": missing},
        )
    return sorted(named, key=_ORDER.index), set(named)


def _ok(cap: C, evidence: str) -> CheckResult:
    return CheckResult(capability=cap.value, result="pass", evidence=evidence)


def _fail(cap: C, evidence: str) -> CheckResult:
    return CheckResult(capability=cap.value, result="fail", evidence=evidence)


def _skip(cap: C, evidence: str) -> CheckResult:
    return CheckResult(capability=cap.value, result="skipped", evidence=evidence)


class _Run:
    def __init__(self, ctx: ServiceContext, session: RobotSession, allow_motion: bool) -> None:
        self.ctx, self.session, self.allow_motion = ctx, session, allow_motion
        self.motion_planned = False
        """True when the motion sequence will run, so `live_position` can retry while cleaning."""
        self.position_pending = False
        self.position_result: CheckResult | None = None

    async def guarded(self, cap: C, check: Callable[[], Awaitable[CheckResult]]) -> CheckResult:
        try:
            return await check()
        except KitError as exc:
            return _fail(cap, f"error {exc.code.value}: {exc.message}")

    def docked(self) -> bool:
        return self.session.state.activity in _DOCKED

    async def settle(self) -> bool:
        """Wait (bounded by `SETTLE_S`) until the robot reports docked or charging."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SETTLE_S
        while not self.docked() and (left := deadline - loop.time()) > 0:
            await self.session.wait_change(self.session.change_seq, left)
        return self.docked()

    async def setting(self, cap: C) -> CheckResult:
        # One queued job for write, read-back and restore: a caller that gives up mid-check
        # (cancelled request, closed client) can't leave the setting changed.
        return await self.session.submit(partial(self._setting_job, cap))

    async def _setting_job(self, cap: C) -> CheckResult:
        session, name = self.session, cap.value.removeprefix("setting.")
        mapping = settings.mapping_for(session, name)
        original = mapping.read(session.reported)
        if original is None:
            return _fail(cap, f"the robot doesn't report {name}")
        session.require_link()  # offline: nothing is written, so there's nothing to restore
        choices = settings.CHOICES[name]
        alternate = choices[(choices.index(original) + 1) % len(choices)]
        # The robot's own keys, so restoring `passes` also restores a latent `twoPass`.
        snapshot = {
            k: copy.deepcopy(v) for k in mapping.group if (v := session.reported.get(k)) is not None
        }
        applied = restored = False
        restore_error = ""
        try:
            change = await settings.write_setting(session, name, alternate)
            applied = change.outcome == "applied"
            wrote = f"{change.outcome} (read back {change.actual})"
        except KitError as exc:
            wrote = f"error {exc.code.value}"
        finally:
            try:
                restored = await settings.publish_confirmed(
                    session, snapshot, lambda r: all(r.get(k) == v for k, v in snapshot.items())
                )
            except Exception as exc:
                code = exc.code.value if isinstance(exc, KitError) else type(exc).__name__
                restore_error = f", error {code}"
        evidence = (
            f"{original} → {alternate}: {wrote}; restored → {original}: "
            f"{'applied' if restored else 'not_applied'}{restore_error} "
            f"(read back {mapping.read(session.reported)})"
        )
        if not restored:
            return _fail(cap, "RESTORE FAILED — set it by hand. " + evidence)
        return (_ok if applied else _fail)(cap, evidence)

    async def command(self, cap: C) -> CheckResult:
        result = await self.session.execute(cap.value)
        evidence = (
            f"{result.outcome} after {result.waited_ms} ms (activity {result.state.activity})"
        )
        return (_ok if result.outcome == "confirmed" else _fail)(cap, evidence)

    async def state(self, cap: C) -> CheckResult:
        value = getattr(self.session.state, _STATE_FIELDS[cap])
        shown = value.model_dump(mode="json") if isinstance(value, KitModel) else value
        if cap is C.STATE_CLEAN_BASE:  # `present` is always set, so alone it proves nothing
            reported = (
                isinstance(value, CleanBaseState) and value.present and value.state is not None
            )
        else:
            reported = shown is not None and (
                not isinstance(shown, dict) or any(v is not None for v in shown.values())
            )
        return (_ok if reported else _fail)(cap, f"reported {json.dumps(shown)}")

    async def position(self, cap: C) -> CheckResult:
        try:
            fix = await self.session.request_position(POSITION_TIMEOUT_S)
        except TimeoutError:
            return _fail(cap, f"no answer within {POSITION_TIMEOUT_S:g} s")
        if fix is not None:
            return _ok(cap, f"fix at ({fix.x_m:.2f}, {fix.y_m:.2f}) m")
        if self.position_pending:  # the retry while cleaning
            return _fail(cap, "no fix while cleaning")
        if self.motion_planned:
            self.position_pending = True
            return _skip(cap, "no fix while docked; retried while cleaning")
        return _skip(cap, "no fix (robot docked or idle): rerun with allow_motion and clean")

    async def cloud(self, cap: C) -> CheckResult:
        if cap not in (C.CLOUD_ROOMS, C.CLOUD_HISTORY):
            return _skip(cap, "not scripted: needs a mission; check it during or after a run")
        try:
            cloud = self.ctx.cloud.transport()
            rid = self.session.robot_id
            found = await (cloud.pmaps(rid) if cap is C.CLOUD_ROOMS else cloud.mission_history(rid))
        except KitError as exc:
            reason = (exc.details or {}).get("reason", exc.code.value)
            return _skip(cap, f"cloud unavailable ({reason})")
        noun = "maps" if cap is C.CLOUD_ROOMS else "missions"
        if not found:  # the cloud transport maps error bodies to an empty list
            return _fail(cap, f"the cloud returned no {noun}")
        return _ok(cap, f"the cloud returned {len(found)} {noun}")

    async def closing_dock(self) -> CheckResult:
        """Send the robot home. Shielded, so a cancelled verify still docks the robot."""
        task = asyncio.ensure_future(self.guarded(C.DOCK, partial(self.command, C.DOCK)))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
        return await asyncio.shield(task)

    async def ready(self) -> bool:
        """Wait (bounded) for any not-ready condition to clear."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + NOT_READY_WAIT_S
        while self.session.state.not_ready is not None:
            if (left := deadline - loop.time()) <= 0:
                return False
            await self.session.wait_change(self.session.change_seq, min(left, 1.0))
        return True

    async def sustained_clean(self) -> CheckResult:
        result = await self.command(C.CLEAN)
        if result.result != "pass":
            return result
        await asyncio.sleep(CLEAN_HOLD_S)
        state = self.session.state
        if state.activity is not Activity.CLEANING or state.mission is None:
            return _fail(
                C.CLEAN,
                f"{result.evidence}, but {CLEAN_HOLD_S:g} s later the robot was "
                f"{state.activity} (mission {'open' if state.mission else 'ended'})",
            )
        return result.model_copy(
            update={"evidence": f"{result.evidence}; still cleaning {CLEAN_HOLD_S:g} s later"}
        )

    async def motion(self, wanted: list[C]) -> list[CheckResult]:
        """Clean from the dock, then pause/resume/stop as named, then dock (always)."""
        if not self.docked():
            why = f"motion checks start from the dock; the robot is {self.session.state.activity}"
            return [_skip(c, why) for c in wanted]
        if not await self.ready():
            nr = self.session.state.not_ready
            why = f"the robot stayed not ready ({nr.code}: {nr.text})" if nr else "not ready"
            return [_skip(c, why) for c in wanted]
        needed = set(wanted) | ({C.PAUSE} if C.RESUME in wanted else set())
        steps = [C.CLEAN, *(c for c in SEQUENCE[1:-1] if c in needed)]
        done: dict[C, CheckResult] = {}
        closing: CheckResult | None = None
        cancelled = False
        try:
            for step in steps:
                if failed := next((d for d in done.values() if d.result != "pass"), None):
                    done[step] = _skip(step, f"not run: {failed.capability} didn't pass")
                    continue
                run = self.sustained_clean if step is C.CLEAN else partial(self.command, step)
                done[step] = await self.guarded(step, run)
                if step is C.CLEAN and done[step].result == "pass" and self.position_pending:
                    self.position_result = await self.guarded(
                        C.LIVE_POSITION, partial(self.position, C.LIVE_POSITION)
                    )
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # Dock only counts as a check when the robot is off the dock when it's sent.
            if not self.docked() or cancelled:
                if C.STOP in done and not cancelled:
                    await asyncio.sleep(STOP_TO_DOCK_S)
                closing = await self.closing_dock()
        failed = next((d for d in done.values() if d.result != "pass"), None)
        if failed is not None:
            sent = f"; sent home anyway: {closing.evidence}" if closing else ""
            done[C.DOCK] = _skip(C.DOCK, f"not checked: {failed.capability} didn't pass{sent}")
        elif closing is None:
            done[C.DOCK] = _fail(C.DOCK, "the robot was already on the dock when dock was due")
        else:
            done[C.DOCK] = closing
        results = [done[c] for c in wanted]
        notes = []
        if C.DOCK not in wanted and closing is not None and closing.result != "pass":
            notes.append("the closing dock failed")
        if closing is not None and not await self.settle():
            notes.append(f"the robot wasn't back on its dock within {SETTLE_S:g} s")
        if notes:
            last = results[-1]
            warning = "; WARNING: " + "; ".join(notes)
            results[-1] = last.model_copy(update={"evidence": last.evidence + warning})
        return results

    async def check(self, cap: C, explicit: bool) -> CheckResult:
        if cap.value.startswith("setting."):
            return await self.guarded(cap, lambda: self.setting(cap))
        if cap is C.FIND:
            return await self.guarded(cap, lambda: self.command(cap))
        if cap in _STATE_FIELDS:
            return await self.state(cap)
        if cap is C.LIVE_POSITION:
            return await self.guarded(cap, lambda: self.position(cap))
        if cap.value.startswith("cloud."):
            return await self.cloud(cap)
        if cap is C.TRAIN and not explicit:
            return _skip(cap, "runs only when named explicitly: it can re-create the map")
        if not self.allow_motion:
            return _skip(cap, "acts on the robot: rerun with allow_motion")
        if cap is C.CLEAN_ROOMS:
            return _skip(cap, "not scripted: run a room clean by hand and record it")
        if not self.docked():  # evac, reboot and train all start from the dock
            return _skip(cap, f"the robot must be docked; it is {self.session.state.activity}")
        result = await self.guarded(cap, lambda: self.command(cap))
        if cap is C.EVAC and not await self.settle():
            warning = f"; WARNING: not back to docked within {SETTLE_S:g} s"
            result = result.model_copy(update={"evidence": result.evidence + warning})
        return result


async def verify(
    ctx: ServiceContext, session: RobotSession, requested: list[str] | None, allow_motion: bool
) -> VerificationRun:
    if (profile := session.profile) is None:
        raise KitError(
            ErrorCode.UNSUPPORTED_MODEL,
            "This robot's model has no profile, so it has nothing to verify.",
            session.robot_id,
        )
    caps, explicit = plan(profile, requested)
    session.require_link()
    started = datetime.now(UTC)
    run = _Run(ctx, session, allow_motion)
    results: dict[C, CheckResult] = {}
    motion = [c for c in caps if c in SEQUENCE]
    run.motion_planned = allow_motion and bool(motion)
    for cap in caps:  # in `_ORDER`, so the motion sequence runs as one block, in its place
        if cap in results:
            continue
        if cap not in SEQUENCE:
            results[cap] = await run.check(cap, cap in explicit)
        elif allow_motion:
            results.update(zip(motion, await run.motion(motion), strict=True))
        else:
            results.update(
                (c, _skip(c, "moves the robot: rerun with allow_motion")) for c in motion
            )
    if run.position_result is not None:
        results[C.LIVE_POSITION] = run.position_result
    elif run.position_pending:
        results[C.LIVE_POSITION] = _skip(C.LIVE_POSITION, "no fix: the robot never cleaned")
    ordered = [results[c] for c in caps]
    ended = datetime.now(UTC)
    row = await ctx.store.execute(
        "INSERT INTO verification_runs"
        " (robot_id, profile_id, started_at, ended_at, allow_motion, results)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            session.robot_id,
            profile.id,
            format_ts(started),
            format_ts(ended),
            int(allow_motion),
            json.dumps([r.model_dump(mode="json") for r in ordered]),
        ),
    )
    anchor = f"run-{started:%Y%m%d-%H%M%S}"
    return VerificationRun(
        id=row.lastrowid or 0,
        robot_id=session.robot_id,
        profile_id=profile.id,
        anchor=anchor,
        started_at=started,
        ended_at=ended,
        allow_motion=allow_motion,
        results=ordered,
        report=report(session, profile, anchor, started, allow_motion, ordered),
    )


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def report(
    session: RobotSession,
    profile: ModelProfile,
    anchor: str,
    started: datetime,
    allow_motion: bool,
    results: list[CheckResult],
) -> str:
    """One run as markdown. No robot ID, name or address: it is meant to be committed."""
    robot = session.robot()
    counts = {r: sum(1 for x in results if x.result == r) for r in ("pass", "fail", "skipped")}
    lines: list[Any] = [
        f'<a id="{anchor}"></a>',
        "",
        f"### {started:%Y-%m-%d %H:%M:%S} UTC · `{profile.id}`",
        "",
        f"- Model: {profile.display_name}, SKU `{robot.sku}`, firmware `{robot.firmware}`",
        f"- Kit {__version__}; motion allowed: {'yes' if allow_motion else 'no'}",
        f"- {counts['pass']} pass, {counts['fail']} fail, {counts['skipped']} skipped",
        f"- Cite as `docs/verification/{profile.id}.md#{anchor}`",
        "",
        "| Capability | Result | Evidence |",
        "|---|---|---|",
        *(f"| `{r.capability}` | {r.result} | {_cell(r.evidence)} |" for r in results),
        "",
    ]
    return "\n".join(lines)
