"""Registry handlers, one module per area. Every module here self-registers on import."""

import importlib
import pkgutil

_loaded = False


def load_operations() -> None:
    """Import every module in this package so its `@operation`s register. Idempotent."""
    global _loaded
    if _loaded:
        return
    for info in pkgutil.walk_packages(__path__, prefix=f"{__name__}."):
        importlib.import_module(info.name)
    _loaded = True
