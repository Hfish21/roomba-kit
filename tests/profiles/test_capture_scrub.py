"""`scripts/capture_fixture.py`: the scrubbing and the service guard. Never touches a robot."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest

from roomba_kit.transport.fake import FakeTransport

SCRIPT = Path(__file__).parents[2] / "scripts" / "capture_fixture.py"
FIXTURES = Path(__file__).parents[1] / "fixtures" / "profiles"

BLID = "A1B2C3D4E5F60718293A4B5C6D7E8F90"
OTHER_HEX = "0123456789abcdef0123456789ABCDEF"
CANARY_IP, CANARY_IP2 = "10.20.30.40", "172.16.99.7"
CANARY_MAC = "a4:5e:60:12:34:56"
CANARY_SECRET = "canary-secret-value"
CANARY_NAME = "Patty"
CANARY_SSID = "HomeNet-Canary"


@pytest.fixture(scope="module")
def capture():
    spec = importlib.util.spec_from_file_location("capture_fixture", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw():
    return {
        "name": CANARY_NAME,
        "sku": "zq9100",
        "softwareVer": "lewis+4.0.4.5+lewis-release+163",
        "hostname": f"Roomba-{BLID}",
        "batPct": 80,
        "passwordHash": CANARY_SECRET,
        "netinfo": {"addr": CANARY_IP, "mask": "255.255.255.0"},
        "wlcfg": {"ssid": CANARY_SSID, "sec": 7},
        "mac": CANARY_MAC,
        "country": "NZ",
        "tz": {"events": [{"dt": 0, "off": 780}]},
        "dock": {"known": True, "id": OTHER_HEX, "fwVer": "4.8.6"},
        "cloudEnv": "prod",
        "connected": True,
        "deeply": {
            "nested": [
                {"wifiSsid": CANARY_SSID, "robotSerialNum": CANARY_SECRET},
                {"note": f"blid {BLID.lower()} at {CANARY_IP2}:8883 mac {CANARY_MAC.upper()}"},
                {"apiToken": CANARY_SECRET, "ownerEmail": "owner@home.example"},
                {"contact": "reach owner@home.example", "other": OTHER_HEX.lower()},
            ],
            "svcEndpoints": {"svcDeplId": "v011"},
            "hwDbgr": None,
        },
        "subModSwVer": {"con": "3.13.0.6-tags/release-3.13.0.7@e9e317d7/"},
        "gateway": CANARY_IP,
    }


def test_scrub_removes_every_canary(capture):
    out = capture.scrub(_raw(), blid=BLID)
    text = json.dumps(out)
    for canary in (
        BLID, BLID.lower(), OTHER_HEX.lower(), CANARY_IP, CANARY_IP2, CANARY_MAC.lower(),
        CANARY_SECRET, CANARY_NAME, CANARY_SSID, "owner@home.example",
    ):  # fmt: skip
        assert canary.lower() not in text.lower(), canary
    for key in ("passwordHash", "netinfo", "wlcfg", "mac", "country", "tz", "cloudEnv"):
        assert key not in out, key
    nested = out["deeply"]["nested"]
    assert nested[0] == {} and nested[2] == {}
    assert "svcEndpoints" not in out["deeply"] and "hwDbgr" not in out["deeply"]


def test_scrub_uses_placeholders_and_keeps_protocol_data(capture):
    out = capture.scrub(_raw(), blid=BLID, robot_id="0" * 31 + "7")
    assert out["hostname"] == "Roomba-" + "0" * 31 + "7"
    assert out["dock"] == {"known": True, "id": "F" * 28 + "0001", "fwVer": "4.8.6"}
    # Placeholders are numbered in document order, so the nested address comes first.
    note = out["deeply"]["nested"][1]["note"]
    assert note == "blid " + "0" * 31 + "7" + " at 192.0.2.1:8883 mac 00:00:5e:00:53:01"
    assert out["gateway"] == "192.0.2.2"
    assert out["deeply"]["nested"][3] == {
        "contact": "reach user@example.com",
        "other": "F" * 28 + "0001",
    }
    assert out["name"] == "Robot"
    # Firmware strings look like dotted quads but aren't addresses.
    assert out["softwareVer"] == "lewis+4.0.4.5+lewis-release+163"
    assert out["subModSwVer"] == {"con": "3.13.0.6-tags/release-3.13.0.7@e9e317d7/"}
    assert (out["sku"], out["batPct"], out["connected"]) == ("zq9100", 80, True)


def test_scrub_does_not_mutate_its_input(capture):
    raw = _raw()
    before = json.dumps(raw, sort_keys=True)
    capture.scrub(raw, blid=BLID)
    assert json.dumps(raw, sort_keys=True) == before


@pytest.mark.parametrize("profile_id", ["roomba_i5", "braava_m6"])
def test_shipped_fixtures_are_already_scrubbed(capture, profile_id):
    """Scrubbing a committed fixture again changes nothing but the display name."""
    state = json.loads((FIXTURES / profile_id / "state.json").read_text())
    out = capture.scrub(state, blid="0" * 31 + "1", name=state["name"])
    assert out == state


def test_refuses_while_the_service_pidfile_is_live(capture, tmp_path):
    pid_file = tmp_path / "service.pid"
    pid_file.write_text(f"{os.getpid()}\n")
    with pytest.raises(SystemExit, match="Stop the service"):
        capture.refuse_if_service_running(pid_file, "http://127.0.0.1:9")
    pid_file.write_text("999999999\n")  # not a live process
    capture.refuse_if_service_running(pid_file, "http://127.0.0.1:9")
    assert pid_file.exists()  # a stale pidfile is left for the service to clean up


def test_resolves_the_robot_by_id_or_name(capture):
    robots = [
        capture.RobotEntry(id="0" * 31 + "1", name="Vacuum", address="192.0.2.10"),
        capture.RobotEntry(id="0" * 31 + "2", name="Mop", address="192.0.2.11"),
    ]
    assert capture.find_robot(robots, "0" * 31 + "2").name == "Mop"
    assert capture.find_robot(robots, "vacuum").id == "0" * 31 + "1"
    with pytest.raises(SystemExit, match="No robot"):
        capture.find_robot(robots, "Nope")


async def test_collect_merges_fragments_then_disconnects(capture, monkeypatch):
    fakes = []

    def factory(robot_id, address, password):
        fakes.append(FakeTransport(robot_id, {"batPct": 90, "bin": {"present": True}}))
        return fakes[-1]

    monkeypatch.setattr(capture, "LocalTransport", factory)
    entry = capture.RobotEntry(id=BLID, name="Vacuum", address="192.0.2.10")

    async def emit_later():
        await asyncio.sleep(0.01)
        fakes[0].emit({"bin": {"full": True}, "batPct": 91})

    task = asyncio.create_task(emit_later())
    reported = await capture.collect(entry, "pw", 0.05)
    await task
    assert reported == {"batPct": 91, "bin": {"present": True, "full": True}}
    assert fakes[0].connected is False
