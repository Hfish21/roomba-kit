"""Shared pytest configuration: hardware opt-in and an isolated kit home."""

from collections.abc import Iterator
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--robot",
        action="store",
        default=None,
        help="Robot ID or name to run `hardware` tests against (needs a running service).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--robot"):
        return
    skip = pytest.mark.skip(reason="hardware test: pass --robot <id|name> to run")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def robot_ref(request: pytest.FixtureRequest) -> str:
    ref = request.config.getoption("--robot")
    if not ref:
        pytest.skip("needs --robot")
    return str(ref)


@pytest.fixture
def tmp_kit_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the kit's config and state dirs at fresh temp dirs."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    monkeypatch.setenv("ROOMBA_KIT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("ROOMBA_KIT_STATE_DIR", str(state_dir))
    yield tmp_path
