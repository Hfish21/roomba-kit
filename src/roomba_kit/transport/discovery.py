"""LAN discovery: the `irobotmcs` UDP datagram to port 5678, answered with JSON by each robot."""

import asyncio
import contextlib
import json
import logging
import socket
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any

from roomba_kit.core.errors import ErrorCode, KitError
from roomba_kit.core.models import DiscoveredRobot

log = logging.getLogger(__name__)

DISCOVERY_PORT = 5678
MAGIC = b"irobotmcs"
RESEND_INTERVAL_S = 1.0

ProfileMatcher = Callable[[str], str | None]
"""SKU → profile ID, or `None` if unsupported. Injected, since transport can't see profiles."""


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.replies: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
        self.send_error: OSError | None = None

    def datagram_received(self, data: bytes, addr: tuple[str | Any, int]) -> None:
        self.replies.put_nowait((data, str(addr[0])))

    def error_received(self, exc: Exception) -> None:
        if isinstance(exc, OSError):
            self.send_error = exc
        log.debug("discovery socket error: %s", exc)


def _blid_from_hostname(hostname: object) -> str | None:
    if not isinstance(hostname, str) or "-" not in hostname:
        return None
    model, _, blid = hostname.partition("-")
    return blid if model.lower() in {"roomba", "irobot"} and blid else None


def parse_reply(data: bytes, source: str, match: ProfileMatcher | None) -> DiscoveredRobot | None:
    """One discovery reply as a `DiscoveredRobot`, or `None` if it isn't one."""
    if data == MAGIC:
        return None  # our own broadcast, looped back
    try:
        obj = json.loads(data)
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    blid = obj.get("robotid") or _blid_from_hostname(obj.get("hostname"))
    if not isinstance(blid, str) or not blid:
        return None
    sku = str(obj.get("sku") or "")
    profile_id = match(sku) if match is not None and sku else None
    return DiscoveredRobot(
        id=blid,
        name=str(obj.get("robotname") or ""),
        sku=sku,
        firmware=str(obj.get("sw") or ""),
        address=str(obj.get("ip") or source),
        supported=profile_id is not None,
        profile_id=profile_id,
    )


@contextlib.asynccontextmanager
async def _endpoint(port: int, bind_port: int | None) -> AsyncIterator[tuple[Any, _Collector]]:
    # Some robots answer to the discovery port rather than the source port, so bind it when free.
    loop = asyncio.get_running_loop()
    candidates = [bind_port] if bind_port is not None else [port, 0]
    for index, candidate in enumerate(candidates):
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                _Collector,
                local_addr=("0.0.0.0", candidate),  # noqa: S104 - must hear broadcast replies
                allow_broadcast=True,
            )
            break
        except OSError:
            if index == len(candidates) - 1:
                raise
    transport.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        yield transport, protocol
    finally:
        transport.close()


async def _exchange(
    target: str, port: int, timeout_s: float, bind_port: int | None
) -> AsyncGenerator[tuple[bytes, str]]:
    """Send `MAGIC` to `target` every second until `timeout_s`, yielding replies as they arrive."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    async with _endpoint(port, bind_port) as (transport, protocol):
        next_send = loop.time()
        while (now := loop.time()) < deadline:
            if now >= next_send:
                transport.sendto(MAGIC, (target, port))
                next_send = now + RESEND_INTERVAL_S
            try:
                async with asyncio.timeout(min(next_send, deadline) - now):
                    reply = await protocol.replies.get()
            except TimeoutError:
                continue
            yield reply
        if protocol.send_error is not None:
            err = protocol.send_error
            raise KitError(
                ErrorCode.INVALID_REQUEST,
                f"Could not send discovery to {target}: {err.strerror or err}. If a VPN owns "
                "the default route, pass the subnet broadcast address (e.g. 192.168.1.255).",
                details={"address": target, "errno": err.errno},
            )


async def discover(
    broadcast: str = "255.255.255.255",
    timeout_s: float = 8.0,
    match: ProfileMatcher | None = None,
    port: int = DISCOVERY_PORT,
    bind_port: int | None = None,
) -> list[DiscoveredRobot]:
    """Every robot that answers a broadcast within `timeout_s`, deduplicated by robot ID."""
    found: dict[str, DiscoveredRobot] = {}
    try:
        async with contextlib.aclosing(_exchange(broadcast, port, timeout_s, bind_port)) as replies:
            async for data, source in replies:
                robot = parse_reply(data, source, match)
                if robot is not None:
                    found.setdefault(robot.id, robot)
    except KitError:
        if not found:
            raise
    return list(found.values())


async def probe(
    address: str,
    timeout_s: float = 3.0,
    match: ProfileMatcher | None = None,
    port: int = DISCOVERY_PORT,
    bind_port: int | None = None,
) -> DiscoveredRobot | None:
    """The robot at `address` if it answers a unicast discovery within `timeout_s`, else `None`."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(address, port, type=socket.SOCK_DGRAM)
    except OSError:
        return None
    resolved = str(infos[0][4][0])
    try:
        async with contextlib.aclosing(_exchange(resolved, port, timeout_s, bind_port)) as replies:
            async for data, source in replies:
                robot = parse_reply(data, source, match)
                if robot is not None and resolved in (source, robot.address):
                    return robot
    except KitError:
        return None
    return None
