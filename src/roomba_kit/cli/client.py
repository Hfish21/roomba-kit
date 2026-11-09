"""The CLI's HTTP client for the service. The CLI never talks to robots (Constitution II)."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

import click
import httpx

from roomba_kit.config import tokens
from roomba_kit.config.settings import load_settings
from roomba_kit.core.errors import CLI_EXIT, ErrorCode, KitError
from roomba_kit.core.registry import Operation, http_request

SERVICE_URL_ENV = "ROOMBA_KIT_SERVICE_URL"
NOT_RUNNING = "Service not running — start it with: roomba-kit service start"
_WILDCARD = {"0.0.0.0", "::", ""}  # noqa: S104 - recognising a bind-all address, not binding


def local_service_url() -> str:
    """The URL of the service this machine's config would run (`server.host`/`server.port`)."""
    try:
        server = load_settings().server
    except KitError:
        return "http://127.0.0.1:8765"
    host = "127.0.0.1" if server.host in _WILDCARD else server.host
    host = f"[{host}]" if ":" in host else host
    return f"http://{host}:{server.port}"


@dataclass
class CliState:
    """Global options, carried on the Click context object."""

    json: bool = False
    quiet: bool = False
    service_url: str | None = None
    transport: httpx.BaseTransport | None = None
    """Test seam: an `httpx.MockTransport` stands in for the service."""

    def url(self) -> str:
        return self.service_url or local_service_url()

    def client(self) -> "ServiceClient":
        return ServiceClient(self.url(), tokens.read_cli_token(), self.transport)


def fail(code: ErrorCode, message: str) -> NoReturn:
    click.echo(message, err=True)
    raise SystemExit(CLI_EXIT[code])


class ServiceClient:
    def __init__(
        self, base_url: str, token: str | None, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.has_token = token is not None
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # No read timeout: wait operations hold the request open for their own `timeout_s`.
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/v1",
            headers=headers,
            transport=transport,
            timeout=httpx.Timeout(5.0, read=None),
        )

    def __enter__(self) -> "ServiceClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self._http.close()

    def call(self, op: Operation, params: Mapping[str, Any]) -> httpx.Response:
        req = http_request(op, params)
        return self.request(req.method, req.path, params=req.query, json=req.body)

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send one request; an unreachable service exits 69 with the start hint."""
        try:
            return self._http.request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            fail(ErrorCode.SERVICE_UNAVAILABLE, NOT_RUNNING)
        except httpx.TransportError as exc:
            fail(ErrorCode.SERVICE_UNAVAILABLE, f"Lost the connection to the service: {exc}")
