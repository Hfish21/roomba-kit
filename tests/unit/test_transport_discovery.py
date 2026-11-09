import asyncio
import json

import pytest

from roomba_kit.transport import discovery
from roomba_kit.transport.discovery import discover, parse_reply, probe

I5 = {
    "robotid": "00000000000000000000000000000001",
    "robotname": "Vacuum",
    "ip": "192.0.2.10",
    "sw": "daredevil+2.6.0",
    "sku": "i5G5020",
    "mac": "00:00:5E:00:53:01",
    "cap": {"pose": 2},
}
M6_HOSTNAME_ONLY = {
    "hostname": "iRobot-00000000000000000000000000000002",
    "robotname": "Mop",
    "ip": "192.0.2.11",
    "sw": "sanmarino+22.29.6",
    "sku": "m611020",
}


def match(sku: str) -> str | None:
    return {"i5G5020": "roomba_i5"}.get(sku)


class Responder(asyncio.DatagramProtocol):
    """A fake robot on 127.0.0.1 answering each `irobotmcs` with `replies`."""

    def __init__(self, replies: list[bytes], ignore_first: int = 0) -> None:
        self.replies = replies
        self.ignore = ignore_first
        self.received = 0

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data, addr) -> None:
        if data != b"irobotmcs":
            return
        self.received += 1
        if self.received <= self.ignore:
            return
        for reply in self.replies:
            self.transport.sendto(reply, addr)


@pytest.fixture
async def responder(request):
    replies, ignore = request.param
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(
        lambda: Responder(replies, ignore), local_addr=("127.0.0.1", 0)
    )
    yield proto, transport.get_extra_info("sockname")[1]
    transport.close()


def test_parse_reply_reads_robotid_or_hostname():
    robot = parse_reply(json.dumps(I5).encode(), "127.0.0.1", match)
    assert robot is not None
    assert (robot.id, robot.name, robot.address, robot.firmware) == (
        I5["robotid"],
        "Vacuum",
        "192.0.2.10",
        "daredevil+2.6.0",
    )
    assert robot.supported and robot.profile_id == "roomba_i5" and not robot.configured
    mop = parse_reply(json.dumps(M6_HOSTNAME_ONLY).encode(), "127.0.0.1", match)
    assert mop is not None and mop.id == "00000000000000000000000000000002"
    assert not mop.supported and mop.profile_id is None


@pytest.mark.parametrize(
    "raw", [b"irobotmcs", b"\xff\xfe", b"not json", b"[1]", b'{"hostname": "printer-1"}']
)
def test_parse_reply_ignores_non_robots(raw):
    assert parse_reply(raw, "127.0.0.1", match) is None


everyone = [
    json.dumps(I5).encode(),
    json.dumps(I5).encode(),
    b"junk",
    json.dumps(M6_HOSTNAME_ONLY).encode(),
]


@pytest.mark.parametrize("responder", [(everyone, 0)], indirect=True)
async def test_discover_dedupes_by_robot_id(responder):
    proto, port = responder
    found = await discover("127.0.0.1", timeout_s=0.3, match=match, port=port, bind_port=0)
    assert sorted(r.id for r in found) == [I5["robotid"], M6_HOSTNAME_ONLY["hostname"][7:]]
    assert proto.received == 1


@pytest.mark.parametrize("responder", [([json.dumps(I5).encode()], 2)], indirect=True)
async def test_discover_resends_until_timeout(responder, monkeypatch):
    monkeypatch.setattr(discovery, "RESEND_INTERVAL_S", 0.05)
    proto, port = responder
    found = await discover("127.0.0.1", timeout_s=0.4, port=port, bind_port=0)
    assert [r.id for r in found] == [I5["robotid"]]
    assert proto.received >= 3


@pytest.mark.parametrize("responder", [([json.dumps(I5).encode()], 0)], indirect=True)
async def test_probe_returns_the_robot_at_the_address(responder):
    _, port = responder
    robot = await probe("127.0.0.1", timeout_s=1.0, match=match, port=port, bind_port=0)
    assert robot is not None and robot.profile_id == "roomba_i5"


@pytest.mark.parametrize("responder", [([], 0)], indirect=True)
async def test_probe_returns_none_when_silent(responder):
    _, port = responder
    assert await probe("127.0.0.1", timeout_s=0.2, port=port, bind_port=0) is None
