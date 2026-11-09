import importlib.util
from pathlib import Path

import pytest

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
