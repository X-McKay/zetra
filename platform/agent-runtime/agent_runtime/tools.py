"""Tool registration with effect classes (spec 11.7), and the decisions a model-free agent makes."""

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# The effects an agent author's tool may have. remote_write is not admitted.
AUTHOR_EFFECTS = {"workspace_read", "workspace_write", "remote_read"}
# The whole vocabulary. `sandbox_execute` belongs to the built-in skill launcher and to no
# tool an author writes (platform/registry/contract/skills.md, section 7.1).
EFFECTS = AUTHOR_EFFECTS | {"sandbox_execute"}
# Tools the runtime provides for skills. An author's tool may not take one of these names.
RESERVED_NAMES = ("skill_use", "skill_file", "skill_run")


@dataclass(frozen=True)
class Tool:
    fn: Callable[..., Awaitable[dict]]
    effect: str

    def __post_init__(self) -> None:
        if self.effect not in EFFECTS:
            raise ValueError(f"effect {self.effect} is not admitted")
        if not inspect.iscoroutinefunction(self.fn) or not self.fn.__doc__:
            raise ValueError("tools must be async callables with a docstring (the description)")


@dataclass(frozen=True)
class ToolCall:
    """`decide` returns this to have the next boundary run the tool `name` with `arguments`."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Done:
    """`decide` returns this to complete the session; `summary` is its short result."""

    summary: str


@dataclass(frozen=True)
class ToolResult:
    """One tool call already made, as `decide` sees it: what was called and what came back,
    exactly as bounded for the session state (a tool's exception is `{"error": <type>}`)."""

    name: str
    arguments: dict[str, Any]
    result: Any
