"""Registry handlers, one module per area. Every module here self-registers on import."""

import importlib
import pkgutil

from roomba_kit.core import registry

_loaded = False


def load_operations() -> None:
    """Import every module in this package so its `@operation`s register. Idempotent."""
    global _loaded
    if _loaded or registry.is_isolated():
        # Inside `registry.isolated()` the real modules would register into the throwaway
        # registry and, being cached in sys.modules, never register again.
        return
    for info in pkgutil.walk_packages(__path__, prefix=f"{__name__}."):
        importlib.import_module(info.name)
    _loaded = True
