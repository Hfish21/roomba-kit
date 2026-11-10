"""The published OpenAPI document is a contract: diffs show changes."""

from roomba_kit.api.app import create_app
from tests.contract._snapshot import as_json, check


def test_openapi_snapshot(tmp_kit_home):
    doc = create_app().openapi()
    assert doc["paths"], "no routes"
    check("openapi.json", as_json(doc))
