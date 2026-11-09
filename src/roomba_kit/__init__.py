"""Roomba Kit: local-first control for iRobot robots."""

from typing import TYPE_CHECKING, Any

__version__ = "0.1.0"

__all__ = ["Client", "__version__"]

if TYPE_CHECKING:
    from roomba_kit.client import Client


def __getattr__(name: str) -> Any:
    # Lazy, so `import roomba_kit.<anything>` doesn't load the client and the whole service.
    if name == "Client":
        from roomba_kit.client import Client

        return Client
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
