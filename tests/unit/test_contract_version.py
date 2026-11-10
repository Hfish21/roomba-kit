"""One kit contract version shared by HTTP, events and MCP."""

from roomba_kit.api import generate
from roomba_kit.core.contract import CONTRACT_VERSION
from roomba_kit.core.events import EVENT_SCHEMA


def test_every_surface_carries_the_contract_version():
    assert generate.API_PREFIX == f"/v{CONTRACT_VERSION}"
    assert EVENT_SCHEMA == f"roomba-kit.event/v{CONTRACT_VERSION}"
