import importlib.util
import json
import shlex
from pathlib import Path

import click
import pytest

from roomba_kit.core import registry
from roomba_kit.service.operations import load_operations

SCRIPT = Path(__file__).parents[2] / "scripts" / "gen_docs.py"


@pytest.fixture(scope="module")
def gen_docs():
    spec = importlib.util.spec_from_file_location("gen_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_output_is_deterministic(gen_docs):
    first = gen_docs.render()
    assert first == gen_docs.render()
    assert set(first) == {"operations.md", "errors.md", "events.md", "support-matrix.md", "cli.md"}


def test_pages_cover_their_sources(gen_docs):
    pages = gen_docs.render()
    assert "`roomba-kit token create [OPTIONS] NAME` (CLI only)" in pages["operations.md"]
    assert (
        "| `service_unavailable` | CLI or stdio MCP can't reach the service | — | 69 |"
        in (pages["errors.md"])
    )
    assert "## `state.changed`" in pages["events.md"]
    assert "| `evac` | — | ✅ |" in pages["support-matrix.md"]
    assert "## `roomba-kit service start`" in pages["cli.md"]


def test_committed_docs_are_current(gen_docs):
    assert gen_docs.main(["--check"]) == 0


def test_check_fails_and_names_stale_files(gen_docs, tmp_path, capsys):
    assert gen_docs.main(["--out", str(tmp_path)]) == 0
    assert gen_docs.main(["--check", "--out", str(tmp_path)]) == 0
    (tmp_path / "errors.md").write_text("edited by hand\n")
    (tmp_path / "cli.md").unlink()
    assert gen_docs.main(["--check", "--out", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "errors.md" in err and "cli.md" in err and "operations.md" not in err


def _parse_cli(argv):
    """Parse a documented command line with the real Click tree, without running it."""
    from roomba_kit.cli.main import cli

    assert argv[0] == "roomba-kit"
    cmd, args = cli, list(argv[1:])
    ctx = click.Context(cli, info_name="roomba-kit")
    while isinstance(cmd, click.Group):
        name = args.pop(0)
        cmd = cmd.get_command(ctx, name)
        assert cmd is not None, argv
        ctx = click.Context(cmd, info_name=name, parent=ctx)
    cmd.make_context(ctx.info_name, args, parent=ctx.parent)


def test_every_operation_has_a_working_example_per_surface(gen_docs):
    """FR-051 / Constitution IX: CLI, curl and MCP examples, each valid for its surface."""
    load_operations()
    page = gen_docs.render()["operations.md"]
    for op in registry.all():
        params = gen_docs.example_params(op)
        registry.parse_params(op, params)  # the placeholders are valid params
        section = page.split(f"## `{op.name}`\n", 1)[1].split("\n## ", 1)[0]
        assert "**Examples**" in section, op.name
        if "cli" in op.surfaces:
            argv = gen_docs.cli_argv(op)
            _parse_cli(argv)
            assert shlex.join(argv) in section, op.name
        if "http" in op.surfaces:
            assert gen_docs.curl_line(op) in section, op.name
        if "mcp" in op.surfaces:
            call = json.loads(gen_docs.mcp_call(op))
            assert call["params"] == {"name": op.name, "arguments": params}, op.name
            assert gen_docs.mcp_call(op) in section, op.name
        else:
            assert '"tools/call"' not in section, op.name


def test_curl_examples_send_what_the_api_expects(gen_docs):
    load_operations()
    assert gen_docs.curl_line(registry.get("health")) == "curl -s http://127.0.0.1:8765/v1/health"
    assert gen_docs.curl_line(registry.get("clean_rooms")) == (
        'curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" '
        '-d \'{"rooms": ["Kitchen"]}\' http://127.0.0.1:8765/v1/robots/Vacuum/commands/clean_rooms'
    )
    assert gen_docs.curl_line(registry.get("events_wait")) == (
        'curl -s -H "Authorization: Bearer $TOKEN" '
        "'http://127.0.0.1:8765/v1/events/wait?since=42'"
    )
    login = gen_docs.cli_argv(registry.get("cloud_login"))
    assert login == ["roomba-kit", "cloud", "login", "--username", "you@example.com"]
