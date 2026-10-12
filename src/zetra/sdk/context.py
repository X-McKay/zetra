"""Build context handed to capability types while a spec is being assembled.

``Agent.from_spec`` instantiates capability types from YAML through ``from_spec``, which
cannot take the package directory or project settings as arguments. The compiler sets
this context for the duration of assembly instead.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .project import Project


@dataclass
class BuildContext:
    package_dir: Path
    project: Project
    gateway_stubs: dict[str, Any] = field(default_factory=dict)  # server -> toolset used in tests
    gateway_token: str | None = None


_CONTEXT: ContextVar[BuildContext | None] = ContextVar("zetra_build_context", default=None)


def current() -> BuildContext:
    ctx = _CONTEXT.get()
    if ctx is None:
        raise RuntimeError("Zetra capability types can only be built by the Zetra compiler")
    return ctx


@contextmanager
def building(ctx: BuildContext):
    token = _CONTEXT.set(ctx)
    try:
        yield ctx
    finally:
        _CONTEXT.reset(token)
