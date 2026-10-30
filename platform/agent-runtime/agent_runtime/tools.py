"""Tool registration with effect classes (spec 11.7)."""

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

EFFECTS = {"workspace_read", "workspace_write", "remote_read"}  # remote_write is not admitted


@dataclass(frozen=True)
class Tool:
    fn: Callable[..., Awaitable[dict]]
    effect: str

    def __post_init__(self) -> None:
        if self.effect not in EFFECTS:
            raise ValueError(f"effect {self.effect} is not admitted")
        if not inspect.iscoroutinefunction(self.fn) or not self.fn.__doc__:
            raise ValueError("tools must be async callables with a docstring (the description)")
