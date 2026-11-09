import json
import os
import signal
import socket
import subprocess
import sys
import textwrap

import pytest
from click.testing import CliRunner

from roomba_kit.cli import lifecycle, main
from roomba_kit.config import paths
from roomba_kit.lifecycle import daemon

FAKE_SERVER = textwrap.dedent(
    """
    import http.server, json, signal, sys
    port, ignore_term = int(sys.argv[1]), sys.argv[2] == "1"
    if ignore_term:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"status": "ok", "version": "fake"}).encode()
            self.send_response(200 if self.path == "/v1/health" else 404)
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    print("fake service up", flush=True)
    http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
    """
)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_kit_home, monkeypatch):
    """A fake `serve` command plus guaranteed cleanup of whatever was spawned."""
    script = tmp_kit_home / "fake_server.py"
    script.write_text(FAKE_SERVER)
    port = free_port()
    monkeypatch.setenv("ROOMBA_KIT_SERVER__PORT", str(port))

    def command(ignore_term=False):
        return [sys.executable, str(script), str(port), "1" if ignore_term else "0"]

    command.url = f"http://127.0.0.1:{port}"
    yield command
    if (pid := daemon.read_pid(paths.pid_file())) and daemon.alive(pid):
        os.kill(pid, signal.SIGKILL)
        daemon.alive(pid)


def start(server, command, **kwargs):
    return daemon.start(
        command, pid_path=paths.pid_file(), log_path=paths.log_file(), url=server.url, **kwargs
    )


def test_start_status_stop(server):
    pid = start(server, server())
    assert daemon.read_pid(paths.pid_file()) == pid
    assert daemon.running_pid(paths.pid_file()) == pid
    assert daemon.health(server.url) == {"status": "ok", "version": "fake"}
    with pytest.raises(daemon.AlreadyRunning):
        start(server, server())
    assert daemon.stop(paths.pid_file()) == pid
    assert not daemon.alive(pid) and not paths.pid_file().exists()
    assert "fake service up" in daemon.tail(paths.log_file())


def test_stop_escalates_to_sigkill(server):
    pid = start(server, server(ignore_term=True))
    assert daemon.stop(paths.pid_file(), timeout=0.3) == pid
    assert not daemon.alive(pid)


def test_start_fails_with_log_tail_when_never_healthy(server):
    command = [sys.executable, "-c", "import time; print('booting', flush=True); time.sleep(30)"]
    with pytest.raises(daemon.DaemonError, match="booting"):
        start(server, command, timeout=0.5)
    assert not paths.pid_file().exists()


def test_start_reports_an_early_exit(server):
    command = [sys.executable, "-c", "import sys; print('bad config'); sys.exit(3)"]
    with pytest.raises(daemon.DaemonError, match="code 3"):
        start(server, command)
    assert not paths.pid_file().exists()


def test_stale_pidfile_is_cleared(tmp_kit_home):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    paths.pid_file().write_text(f"{proc.pid}\n")
    assert daemon.running_pid(paths.pid_file()) is None
    assert not paths.pid_file().exists()
    assert daemon.stop(paths.pid_file()) is None


def test_follow_yields_appended_lines(tmp_kit_home):
    log = paths.log_file()
    log.write_text("old\n")
    lines = daemon.follow(log, poll_s=0.01)
    with log.open("a") as fh:
        fh.write("new\n")
    assert next(lines) == "new\n"


def test_cli_lifecycle(server, monkeypatch):
    monkeypatch.setattr(lifecycle, "serve_command", server)
    run = CliRunner().invoke
    started = run(main.cli, ["--json", "service", "start"])
    assert started.exit_code == 0, started.output
    pid = json.loads(started.stdout)["pid"]

    status = run(main.cli, ["--json", "service", "status"])
    assert status.exit_code == 0
    assert json.loads(status.stdout)["pid"] == pid

    assert "Already running" in run(main.cli, ["service", "start"]).stdout
    assert "fake service up" in run(main.cli, ["service", "logs"]).stdout

    stopped = run(main.cli, ["service", "stop"])
    assert stopped.stdout == f"Service stopped (pid {pid}).\n"
    assert run(main.cli, ["service", "status"]).exit_code == 69


def test_config_path_and_version(tmp_kit_home):
    result = CliRunner().invoke(main.cli, ["--json", "config", "path"])
    assert json.loads(result.stdout) == {
        "config_dir": str(paths.config_dir()),
        "state_dir": str(paths.state_dir()),
    }
    assert CliRunner().invoke(main.cli, ["version"]).stdout.startswith("roomba-kit ")
