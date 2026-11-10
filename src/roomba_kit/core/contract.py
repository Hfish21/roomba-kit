"""The kit's contract version (docs/contracts/surfaces.md "Versioning").

One number covers the HTTP API (`/v1`), the event schema (`roomba-kit.event/v1`) and the MCP tool
set (reported as the MCP server version). A breaking change to any of them bumps it.
"""

CONTRACT_VERSION = "1"
API_PREFIX = f"/v{CONTRACT_VERSION}"
