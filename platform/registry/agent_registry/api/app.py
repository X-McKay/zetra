"""The application. `create_app` is the one place the pieces are joined."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import FastAPI

from agent_registry.api import cluster, errors, handover, read, write
from agent_registry.api import schemas as s
from agent_registry.api.auth import TokenVerifier
from agent_registry.config import Settings
from agent_registry.store import Store


def _utcnow() -> datetime:
    return datetime.now(UTC)


def create_app(
    store: Store,
    settings: Settings,
    verifier: TokenVerifier,
    *,
    clock: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    app = FastAPI(title="Agent registry", version="1", docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.settings = settings
    app.state.verifier = verifier
    app.state.clock = clock
    errors.install(app)

    @app.get("/healthz", response_model=s.Health, tags=["health"])
    def healthz() -> s.Health:
        """No token needed. Answers 503 `unavailable` when the database cannot be reached."""
        store.ping()
        return s.Health(status="ok", mode=store.meta().mode)

    app.include_router(read.router)
    app.include_router(write.router)
    app.include_router(handover.router)
    app.include_router(cluster.router)
    return app
