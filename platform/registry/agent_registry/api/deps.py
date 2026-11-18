"""What a handler may ask for besides the caller."""

from __future__ import annotations

from datetime import datetime

from fastapi import Request

from agent_registry.api.errors import ApiError
from agent_registry.config import Settings
from agent_registry.store import Store


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def now(request: Request) -> datetime:
    """The time, from the application's clock. Handlers never call `datetime.now` themselves,
    so a test can set the time."""
    return request.app.state.clock()


def writable(request: Request) -> None:
    """Every write route but leave-recovery depends on this."""
    meta = request.app.state.store.meta()
    if meta.mode == "recovery":
        raise ApiError(
            "in-recovery",
            f"the registry is in recovery ({meta.mode_reason}); nothing is written until "
            "recovery is left",
        )
