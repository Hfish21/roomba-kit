"""API token admin: CLI-only local file operations (plan Complexity Tracking).

Creating a token shows its plaintext once, so these never go over HTTP or MCP. They run in the
CLI process against `credentials.toml`; the service picks changes up on its next read.
"""

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, Field

from roomba_kit.config.tokens import TokenStore
from roomba_kit.core.models import KitModel
from roomba_kit.core.registry import Cli, operation


class TokenContext(Protocol):
    """What token ops need. `ServiceContext` and the CLI's local context both provide it."""

    @property
    def version(self) -> str: ...

    @property
    def tokens(self) -> TokenStore: ...


class TokenName(BaseModel):
    name: str = Field(description="Token name, e.g. `cli` or `home-assistant`")


class TokenCreated(KitModel):
    name: str
    token: str = Field(description="The plaintext token. Shown once; store it now.")


class TokenRecord(KitModel):
    name: str
    created_at: datetime
    last_used_at: datetime | None = None


class TokenRevoked(KitModel):
    name: str
    revoked: bool


@operation(
    description="Create an API token and show it once. The `cli` token is also saved for the CLI.",
    params=TokenName,
    result=TokenCreated,
    cli=Cli(("token",), "create", args=("name",)),
    surfaces={"cli"},
    local=True,
)
async def token_create(ctx: TokenContext, params: TokenName) -> TokenCreated:
    return TokenCreated(name=params.name, token=ctx.tokens.create(params.name))


@operation(
    description="List API tokens: name, created and last used. Token values are never shown.",
    result=list[TokenRecord],
    read_only=True,
    cli=Cli(("token",), "list"),
    surfaces={"cli"},
    local=True,
)
async def token_list(ctx: TokenContext, params: BaseModel) -> list[TokenRecord]:
    return [
        TokenRecord(name=t.name, created_at=t.created_at, last_used_at=t.last_used_at)
        for t in sorted(ctx.tokens.list(), key=lambda t: t.name)
    ]


@operation(
    description="Revoke an API token. The service rejects it from its next request.",
    params=TokenName,
    result=TokenRevoked,
    cli=Cli(("token",), "revoke", args=("name",)),
    surfaces={"cli"},
    local=True,
)
async def token_revoke(ctx: TokenContext, params: TokenName) -> TokenRevoked:
    return TokenRevoked(name=params.name, revoked=ctx.tokens.revoke(params.name))
