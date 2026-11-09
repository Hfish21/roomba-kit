"""Service components: derived behaviour that plugs into the running service.

A component is an async `start(ctx)` that returns an optional async `stop()`. Story modules
register one with `@component("name")` in their own file; `load_components()` imports every
`roomba_kit.service` module, so adding a component never edits the composition root.
"""

import importlib
import pkgutil
from collections.abc import Awaitable, Callable
from typing import Any

Stop = Callable[[], Awaitable[None]]
Start = Callable[[Any], Awaitable[Stop | None]]

_COMPONENTS: dict[str, Start] = {}


def component(name: str) -> Callable[[Start], Start]:
    def decorate(start: Start) -> Start:
        if name in _COMPONENTS:
            raise ValueError(f"service component {name!r} is registered twice")
        _COMPONENTS[name] = start
        return start

    return decorate


def load_components() -> dict[str, Start]:
    """Import every service module (components self-register) and return them by name."""
    import roomba_kit.service as service

    for info in pkgutil.walk_packages(service.__path__, prefix=f"{service.__name__}."):
        if info.name.startswith(f"{service.__name__}.operations"):
            continue
        importlib.import_module(info.name)
    return dict(sorted(_COMPONENTS.items()))
