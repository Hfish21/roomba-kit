"""No credential appears anywhere the kit writes.

Canary robot passwords, cloud logins and an API token are configured through `credentials.toml`
and env overrides, handed out by the fake password retriever and the fake cloud, and sent as op
parameters. Then every registered operation runs on every surface it's on (HTTP, MCP, CLI with
and without `--json`), once with valid params and once on an error path. Everything the kit
emitted is scanned: log records from every logger at DEBUG (non-propagating ones included),
CLI stdout/stderr, HTTP bodies and headers, MCP results and errors, every bus event and the
raw SSE stream, the process's own stdout/stderr, and the bytes of `state.db` and `config.toml`.
`credentials.toml` is the one place allowed to hold them.
"""

import asyncio
import dataclasses
import hashlib
import itertools
import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client

from roomba_kit.cli.main import build_cli
from roomba_kit.config import paths
from roomba_kit.config.credentials import CredentialStore
from roomba_kit.config.settings import load_settings
from roomba_kit.core import registry
from roomba_kit.core.events import Event
from roomba_kit.lifecycle import daemon
from roomba_kit.mcp.server import build_mcp, in_process
from roomba_kit.service import cloud, cloud_data, confirm, settings
from roomba_kit.service.app import ServiceContext
from roomba_kit.service.operations import load_operations, setup
from roomba_kit.transport.fake import FakeCloud, FakePasswordRetriever, FakeTransportFactory
from tests.integration.helpers import (
    FAST_BACKOFF,
    FIXTURES,
    I5,
    M6,
    AsgiBridge,
    api_client,
    cli_args,
    configure_robots,
    http_call,
    run_cli,
    serving,
    until,
)
from tests.integration.test_us1_setup_and_commands import ODD, discover, probe
from tests.unit.test_sse import Stream

I5_FILE_PW = "canary-i5-file-pw-q7Xv2"
M6_ENV_PW = "canary-m6-env-pw-L3k9d"
CLOUD_FILE_USER = "canary-file-user-8Hn2@example.com"
CLOUD_FILE_PW = "canary-file-cloud-pw-Z4p1"
CLOUD_ENV_PW = "canary-env-cloud-pw-T6w3"
API_TOKEN = "rk_canaryTokenValue_0123456789abcdefXYZ"
RETRIEVED_PW = "canary-retrieved-pw-M2c8"
CLOUD_ROBOT_PW = "canary-cloud-robot-pw-R5j0"
SET_PW = "canary-set-pw-V1b7"
LOGIN_USER = "canary-login-user-4Gq@example.com"
LOGIN_PW = "canary-login-pw-Y8e2"
CANARIES = [
    I5_FILE_PW, M6_ENV_PW, CLOUD_FILE_USER, CLOUD_FILE_PW, CLOUD_ENV_PW, API_TOKEN,
    RETRIEVED_PW, CLOUD_ROBOT_PW, SET_PW, LOGIN_USER, LOGIN_PW,
]  # fmt: skip
PUBLISHED = "01M0J6GSQ8MB898573DF8CCQNN"
"""A mission in the i5 cloud-history fixture with a published coverage map."""
TOKEN_SHOWN = "cli token_create stdout (shows the new token once)"

Params = dict[str, Any] | Callable[["Run"], dict[str, Any]]
# Every registered operation, in run order: (valid params, error-path params). An error of
# `None` means no param-level error exists; the error path is then an unauthenticated call.
PLAN: list[tuple[str, Params, dict[str, Any] | None]] = [
    ("health", {}, None),
    ("discover_robots", {"timeout_s": 0.1}, {"broadcast": "not-an-ip"}),
    ("add_robot", {"address": "192.0.2.12"}, {"address": "192.0.2.99"}),
    ("retrieve_password", {"robot": "Vacuum", "timeout_s": 5}, {"robot": "nobody"}),
    ("set_robot_password", {"robot": I5, "password": SET_PW}, {"robot": "x", "password": SET_PW}),
    ("cloud_login", {"username": LOGIN_USER, "password": LOGIN_PW}, {"password": LOGIN_PW}),
    ("cloud_status", {}, None),
    # The Mop's password comes from env, so storing one is refused.
    ("cloud_retrieve_password", {"robot": "Vacuum"}, {"robot": "Mop"}),
    ("service_status", {}, None),
    ("list_robots", {}, None),
    ("get_robot", {"robot": "Vacuum"}, {"robot": "nobody"}),
    ("get_state", {"robot": M6}, {"robot": "nobody"}),
    ("get_capabilities", {"robot": "Mop"}, {"robot": "nobody"}),
    ("import_rooms", {"robot": I5}, {"robot": "nobody"}),
    ("list_rooms", {"robot": I5}, {"robot": "nobody"}),
    (
        "name_room",
        {"robot": I5, "room_id": "7", "name": "Utility"},
        {"robot": I5, "room_id": "no", "name": "x"},
    ),
    ("clean_rooms", {"robot": I5, "rooms": ["utility"]}, {"robot": I5, "rooms": []}),
    ("clean", {"robot": "Mop"}, {"robot": "nobody"}),
    ("pause", {"robot": "Mop"}, {"robot": "nobody"}),
    ("get_position", {"robot": M6}, {"robot": I5}),
    ("resume", {"robot": "Mop"}, {"robot": "nobody"}),
    ("stop", {"robot": "Mop"}, {"robot": "nobody"}),
    ("dock", {"robot": I5}, {"robot": "nobody"}),
    ("evac", {"robot": I5}, {"robot": M6}),
    ("find", {"robot": M6}, {"robot": "nobody"}),
    ("reboot", {"robot": M6}, {"robot": "nobody"}),
    ("train", {"robot": I5}, {"robot": "nobody"}),
    ("get_settings", {"robot": I5}, {"robot": "nobody"}),
    (
        "set_setting",
        {"robot": I5, "setting": "passes", "value": "two"},
        {"robot": I5, "setting": "pad_wetness", "value": 2},
    ),
    ("events_list", {"since": 0, "limit": 1000}, {"robot": "nobody"}),
    ("events_wait", {"since": 0, "timeout_s": 0.1}, {"since": 0, "robot": "nobody"}),
    ("mission_wait", {"robot": "Mop", "timeout_s": 0.1}, {"robot": "nobody"}),
    ("list_missions", {}, {"outcome": "bogus"}),
    ("get_mission", lambda run: {"mission_id": run.missions[0]}, {"mission_id": "nope"}),
    ("cloud_history", {"robot": I5}, {"robot": "nobody"}),
    ("get_floor_plan", {"robot": I5}, {"robot": "nobody"}),
    (
        "get_coverage",
        {"robot": I5, "mission_id": PUBLISHED},
        {"robot": "nobody", "mission_id": "x"},
    ),
    (
        "verify",
        {"robot": "Mop", "capabilities": ["find"]},
        {"robot": "Mop", "capabilities": ["evac"]},
    ),
    ("config_show", {}, None),
    ("token_create", lambda run: {"name": f"leak-{next(run.names)}"}, {"name": "bad name!"}),
    ("token_list", {}, None),
    ("token_revoke", {"name": "leak-0"}, None),
    ("cloud_logout", {}, None),
    ("remove_robot", {"robot": ODD}, None),  # an unknown robot answers removed=false
]
CLI_BAD_TOKEN_EXIT = {"service_status": 0}
"""`service status` is a lifecycle composite: it reports health and omits what a 401 hides."""


@dataclasses.dataclass
class Capture:
    """Everything the kit emitted, as (channel, text), plus every log record."""

    items: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    records: list[logging.LogRecord] = dataclasses.field(default_factory=list)

    def add(self, channel: str, text: str | bytes) -> None:
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        self.items.append((channel, text))

    def leaks(self, canaries: list[str]) -> list[tuple[str, str]]:
        logs = [
            (f"log {r.name}", " ".join(filter(None, (r.getMessage(), r.exc_text, r.stack_info))))
            for r in list(self.records)
        ]
        return [(ch, c) for ch, text in [*self.items, *logs] for c in canaries if c in text]


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[Capture]:
    """Records via `Logger.handle`, which every logger calls for every enabled record whether or
    not it propagates, after the record factory (where the kit redacts) has run."""
    cap = Capture()
    handle = logging.Logger.handle

    def recording(self: logging.Logger, record: logging.LogRecord) -> None:
        cap.records.append(record)
        handle(self, record)

    monkeypatch.setattr(logging.Logger, "handle", recording)
    loggers = [logging.root, *logging.root.manager.loggerDict.values()]
    levels = {lg: lg.level for lg in loggers if isinstance(lg, logging.Logger)}
    for logger in levels:
        logger.setLevel(logging.DEBUG)
    factory = logging.getLogRecordFactory()
    yield cap
    logging.setLogRecordFactory(factory)
    for logger, level in levels.items():
        logger.setLevel(level)


@dataclasses.dataclass
class Run:
    """Runs ops on each surface, capturing output and checking valid/error outcomes."""

    capture: Capture
    client: httpx.AsyncClient
    anonymous: httpx.AsyncClient
    mcp: Client[Any]
    bridge: AsgiBridge
    root: Any = dataclasses.field(default_factory=build_cli)
    failures: list[str] = dataclasses.field(default_factory=list)
    created_tokens: list[str] = dataclasses.field(default_factory=list)
    missions: list[str] = dataclasses.field(default_factory=list)
    names: Iterator[int] = dataclasses.field(default_factory=itertools.count)

    def check(self, op: str, path: str, ok: bool, detail: object) -> None:
        if not ok:
            self.failures.append(f"{op} {path}: {str(detail)[:300]}")

    async def http(self, op: str, params: dict[str, Any], *, anonymous: bool = False) -> int:
        response = await http_call(self.anonymous if anonymous else self.client, op, params)
        self.capture.add(f"http {op} body", response.content)
        self.capture.add(f"http {op} headers", json.dumps(response.headers.multi_items()))
        return response.status_code

    async def mcp_call(self, op: str, params: dict[str, Any]) -> bool:
        result = await self.mcp.call_tool(op, params, raise_on_error=False)
        texts = [getattr(c, "text", "") for c in result.content]
        self.capture.add(f"mcp {op}", json.dumps([texts, result.structured_content]))
        return bool(result.is_error)

    async def cli(self, op: str, params: dict[str, Any], *flags: str, **env: str) -> int:
        args, stdin = cli_args(registry.get(op), params)
        result = await run_cli(self.root, [*flags, *args], self.bridge, input=stdin, env=env)
        if op == "token_create" and result.exit_code == 0:
            if "--json" in flags:
                self.created_tokens.append(json.loads(result.stdout)["token"])
            self.capture.add(TOKEN_SHOWN, result.stdout)
        else:
            self.capture.add(f"cli {op} {flags} stdout", result.stdout)
        self.capture.add(f"cli {op} {flags} stderr", result.stderr)
        return result.exit_code

    async def every_surface(self, name: str, valid: Params, error: dict[str, Any] | None) -> None:
        op = registry.get(name)
        authed = op.http is not None and op.http.auth

        def params() -> dict[str, Any]:
            return valid(self) if callable(valid) else valid

        if "http" in op.surfaces:
            status = await self.http(name, params())
            self.check(name, "http valid", status < 400, status)
            if error is not None:
                status = await self.http(name, error)
                self.check(name, "http error", status >= 400, status)
            elif authed:
                status = await self.http(name, params(), anonymous=True)
                self.check(name, "http no token", status == 401, status)
        if "mcp" in op.surfaces:
            self.check(name, "mcp valid", not await self.mcp_call(name, params()), "tool error")
            if error is not None:
                self.check(name, "mcp error", await self.mcp_call(name, error), "no tool error")
        if "cli" in op.surfaces:
            for flags in (("--json",), ()):
                code = await self.cli(name, params(), *flags)
                self.check(name, f"cli valid {flags}", code in (0, 3), code)
            if error is not None:
                code = await self.cli(name, error, "--json")
                self.check(name, "cli error", code != 0, code)
            elif authed:
                code = await self.cli(name, params(), ROOMBA_KIT_API_TOKEN="rk_wrong")
                self.check(name, "cli bad token", code == CLI_BAD_TOKEN_EXIT.get(name, 77), code)


def configure_canaries(monkeypatch: pytest.MonkeyPatch) -> None:
    configure_robots(i5_password=I5_FILE_PW, m6_password=None)
    creds = CredentialStore()
    creds.set_cloud(CLOUD_FILE_USER, CLOUD_FILE_PW)
    with creds.edit() as data:
        digest = hashlib.sha256(API_TOKEN.encode()).hexdigest()
        token = {"name": "canary", "sha256": digest, "created_at": "2026-11-08T00:00:00Z"}
        data.setdefault("tokens", []).append(token)
    monkeypatch.setenv(f"ROOMBA_KIT_ROBOTS__{M6}__PASSWORD", M6_ENV_PW)
    monkeypatch.setenv("ROOMBA_KIT_CLOUD__PASSWORD", CLOUD_ENV_PW)
    monkeypatch.setenv("ROOMBA_KIT_API_TOKEN", API_TOKEN)


def shorten_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commands the fakes don't act on (stop, reboot, train) come back unconfirmed quickly."""
    for name, spec in list(confirm.DEFAULT_SPECS.items()):
        monkeypatch.setitem(confirm.DEFAULT_SPECS, name, dataclasses.replace(spec, window_s=0.3))
    monkeypatch.setattr(settings, "CONFIRM_TIMEOUT_S", 0.3)
    monkeypatch.setattr(setup, "RETRY_S", 0.01)
    monkeypatch.setattr(cloud, "POLL_S", 0.02)
    monkeypatch.setattr(cloud_data, "RECONCILE_S", 0.02)
    # `service status` probes the port before asking the service; this service is in-process.
    monkeypatch.setattr(daemon, "health", lambda url, timeout=1.0: {"status": "ok"})


def kit_files() -> list[Path]:
    """`config.toml`, `state.db` (and its WAL) and anything else the kit wrote, except secrets."""
    found = [*paths.config_dir().rglob("*"), *paths.state_dir().rglob("*")]
    secret = paths.credentials_file().name
    return sorted(p for p in found if p.is_file() and not p.name.startswith(secret))


async def test_no_canary_appears_anywhere(tmp_kit_home, capture, capfd, monkeypatch):
    load_operations()
    assert {name for name, *_ in PLAN} == {op.name for op in registry.all()}, "plan every op"
    shorten_waits(monkeypatch)
    configure_canaries(monkeypatch)

    factory = FakeTransportFactory.from_fixtures(FIXTURES)
    fake_cloud = FakeCloud.from_fixtures(
        FIXTURES, passwords={I5: CLOUD_ROBOT_PW, M6: CLOUD_ROBOT_PW}
    )
    doc = json.loads((FIXTURES / "captures" / "i5_floorplan_umf.json").read_text())
    header = doc["maps"][0]["map_header"]
    fake_cloud.umf_by_key[(I5, header["id"], header["version"])] = doc
    logins: list[tuple[str, str]] = []

    def cloud_factory(username: str, password: str, country: str) -> FakeCloud:
        logins.append((username, password))
        return fake_cloud

    retriever = FakePasswordRetriever({"192.0.2.10": RETRIEVED_PW})
    ctx = ServiceContext(
        settings=load_settings(),
        transport_factory=factory,
        password_retriever=retriever,
        cloud_factory=cloud_factory,
        discover=discover,
        probe=probe,
        session_options=FAST_BACKOFF,
    )
    events: list[Event] = []

    async def on_event(event: Event) -> None:
        events.append(event)

    async with serving(ctx) as app:
        ctx.bus.subscribe(on_event)
        await until(lambda: ctx.cloud.status().status == "connected")
        sse = Stream(app, API_TOKEN, "", accept="text/event-stream")
        async with (
            sse,
            api_client(app, API_TOKEN) as client,
            api_client(app, None) as anonymous,
            Client(build_mcp(in_process(ctx))) as mcp,
        ):
            run = Run(capture, client, anonymous, mcp, AsgiBridge(app, asyncio.get_running_loop()))
            for name, valid, error in PLAN:
                if name == "get_mission":
                    page = await http_call(client, "list_missions", {})
                    run.missions = [m["id"] for m in page.json()]
                    assert run.missions, "the commands above should have recorded a mission"
                await run.every_surface(name, valid, error)
            await asyncio.sleep(0.2)  # let trailing events reach the bus and the stream
        while not sse._chunks.empty():
            capture.add("sse", sse._chunks.get_nowait())
    for event in events:
        capture.add(f"event {event.type.value}", event.model_dump_json())
    out, err = capfd.readouterr()
    capture.add("process stdout", out)
    capture.add("process stderr", err)
    for path in kit_files():
        capture.add(f"file {path.name}", path.read_bytes())

    assert not run.failures, "\n".join(run.failures)
    # The canaries were in play, not just configured.
    assert factory.get(M6).password == M6_ENV_PW and factory.get(I5).password == CLOUD_ROBOT_PW
    assert (CLOUD_FILE_USER, CLOUD_ENV_PW) in logins and (LOGIN_USER, CLOUD_ENV_PW) in logins
    assert retriever.calls and "robot_password" in fake_cloud.calls
    assert CLOUD_ROBOT_PW in paths.credentials_file().read_text()
    channels = {channel for channel, _ in capture.items}
    assert {"file config.toml", "file state.db", "sse", TOKEN_SHOWN} <= channels
    assert len(capture.records) > 100 and run.created_tokens

    leaks = capture.leaks(CANARIES)
    leaks += [(ch, t) for ch, t in capture.leaks(run.created_tokens) if ch != TOKEN_SHOWN]
    assert not leaks, leaks

    # Self-check: the log scan is live. With redaction a canary is masked even on a logger that
    # doesn't propagate; with the redaction factory removed, the same capture finds it.
    quiet = logging.getLogger("uvicorn.error")
    monkeypatch.setattr(quiet, "propagate", False)
    quiet.warning("robot answered %s", M6_ENV_PW)
    assert capture.records[-1].name == "uvicorn.error"
    assert "***" in capture.records[-1].getMessage() and not capture.leaks([M6_ENV_PW])
    logging.setLogRecordFactory(logging.LogRecord)
    quiet.warning("robot answered %s", M6_ENV_PW)
    assert capture.leaks([M6_ENV_PW]) == [("log uvicorn.error", M6_ENV_PW)]
