"""The application. `create_app` is the one place the pieces are joined."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, TypeVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from session_api import errors, record
from session_api.auth import AuthError, Principal, TokenVerifier, kinds
from session_api.config import Settings
from session_api.errors import ApiError
from session_api.ports import Objects, Reports, Sessions
from session_api.service import SessionService

log = logging.getLogger("session_api")
M = TypeVar("M", bound=BaseModel)

START = "POST /v1/tenants/{tenant}/sessions"
READ = "GET /v1/tenants/{tenant}/sessions/{agent}/{sessionId}"
CANCEL = "POST /v1/tenants/{tenant}/sessions/{agent}/{sessionId}/cancel"
LIST = "GET /v1/tenants/{tenant}/sessions"
CANCEL_RELEASE = "POST /v1/tenants/{tenant}/sessions/cancel"


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: str
    task: Any  # required, though any JSON value, null included
    release: str | None = None
    sessionId: str | None = None
    purpose: str = "task"


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)


class CancelReleaseBody(CancelBody):
    agent: str
    release: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _body(request: Request, model: type[M]) -> M:
    try:
        return model.model_validate_json(await request.body())
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ()))
        raise ApiError("invalid-arguments", f"{where or 'body'}: {first['msg']}") from None


def create_app(
    settings: Settings,
    verifier: TokenVerifier,
    sessions: Sessions,
    objects: Objects,
    reports: Reports,
    *,
    clock: Callable[[], datetime] = _utcnow,
) -> FastAPI:
    app = FastAPI(title="Session interface", version="1", docs_url=None, redoc_url=None)
    service = SessionService(settings, sessions, objects, reports)
    errors.install(app)

    @asynccontextmanager
    async def audited(route: str, line: bool = True) -> AsyncIterator[record.Entry]:
        """One record line per call (not for a read or a list), whatever the answer. An error nobody planned for leaves
        as `unavailable` with nothing of it in the answer or the log but its type."""
        entry = record.Entry(route)
        try:
            yield entry
        except ApiError as error:
            entry.code = error.code
            raise
        except Exception as error:  # noqa: BLE001
            log.error("unexpected %s", type(error).__name__)
            entry.code = "unavailable"
            raise ApiError("unavailable", "the request could not be completed") from None
        finally:
            if line:
                record.write(entry, clock())

    async def caller(
        request: Request, tenant: str, allowed: tuple[str, ...], entry: record.Entry
    ) -> tuple[Principal, str]:
        """The tenant, then the token, then the role. The first check that fails answers."""
        if tenant != settings.tenant:
            raise ApiError("not-found", f"no tenant {tenant}")
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ApiError("not-signed-in", "a bearer token is required")
        try:
            principal = await asyncio.to_thread(verifier.verify, token.strip())
        except AuthError as error:
            raise ApiError("not-signed-in", f"the token was not accepted: {error}") from None
        entry.caller = principal.actor
        for kind in kinds(principal, settings):
            if kind in allowed:
                entry.role = kind
                return principal, kind
        raise ApiError.forbidden("this token may not do this")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/v1/tenants/{tenant}/sessions")
    async def start(request: Request, tenant: str) -> JSONResponse:
        async with audited(START) as entry:
            principal, kind = await caller(request, tenant, ("operator", "evaluator"), entry)
            body = await _body(request, StartBody)
            answer, status = await service.start(kind, principal.actor, body, entry)
            return JSONResponse(answer, status_code=status)

    @app.post("/v1/tenants/{tenant}/sessions/cancel")
    async def cancel_release(request: Request, tenant: str) -> dict:
        async with audited(CANCEL_RELEASE) as entry:
            await caller(request, tenant, ("operator", "provisioner"), entry)
            body = await _body(request, CancelReleaseBody)
            return await service.cancel_release(body.agent, body.release, entry)

    @app.get("/v1/tenants/{tenant}/sessions")
    async def list_sessions(
        request: Request,
        tenant: str,
        state: str | None = None,
        agent: str | None = None,
        release: str | None = None,
    ) -> dict:
        async with audited(LIST, False) as entry:
            await caller(request, tenant, ("operator", "provisioner"), entry)
            return await service.list_running(state, agent, release)

    @app.get("/v1/tenants/{tenant}/sessions/{agent}/{session_id}")
    async def read(request: Request, tenant: str, agent: str, session_id: str) -> dict:
        async with audited(READ, False) as entry:
            _, kind = await caller(request, tenant, ("operator", "evaluator"), entry)
            return await service.read(kind, agent, session_id, entry)

    @app.post("/v1/tenants/{tenant}/sessions/{agent}/{session_id}/cancel")
    async def cancel(request: Request, tenant: str, agent: str, session_id: str) -> dict:
        async with audited(CANCEL) as entry:
            _, kind = await caller(request, tenant, ("operator", "evaluator"), entry)
            await _body(request, CancelBody)
            return await service.cancel(kind, agent, session_id, entry)

    return app
