"""The tenant's session interface: cancel a revoked release's sessions, and list and cancel an
agent's running sessions for an alias-move hold.

`platform/session-api/contract/sessions.md` section 4.5. It carries the registry's token and
logs none.
"""

from __future__ import annotations

import logging
from typing import Protocol
from urllib.parse import quote

import httpx

from .registry import TIMEOUT, HttpRegistry, RegistryError

log = logging.getLogger("registry_provisioner")


class SessionPort(Protocol):
    def cancel_release(self, tenant: str, agent: str, release: str, reason: str) -> int:
        """Cancel the running sessions of one release; the number cancelled.

        Raises RegistryError when the call failed or any cancellation did.
        """
        ...

    def cancel_sessions(self, tenant: str, agent: str, purpose: str, reason: str) -> int:
        """Cancel the agent's running sessions of one purpose, of every release; the number
        cancelled. Raises RegistryError when the call failed or any cancellation did."""
        ...

    def list_running(self, tenant: str, agent: str) -> list[dict]:
        """The agent's running sessions, each with at least `sessionId` and `purpose`.

        Raises RegistryError when the list could not be read.
        """
        ...


class HttpSessions:
    def __init__(self, template: str, registry: HttpRegistry) -> None:
        self._template = template
        self._tokens = registry.tokens
        self._http = registry.http

    def _url(self, tenant: str, path: str) -> str:
        base = self._template.replace("{tenant}", quote(tenant, safe="")).rstrip("/")
        return base + f"/v1/tenants/{quote(tenant, safe='')}/sessions{path}"

    def cancel_release(self, tenant: str, agent: str, release: str, reason: str) -> int:
        return self._cancel(tenant, {"agent": agent, "release": release, "reason": reason})

    def cancel_sessions(self, tenant: str, agent: str, purpose: str, reason: str) -> int:
        return self._cancel(tenant, {"agent": agent, "purpose": purpose, "reason": reason})

    def _cancel(self, tenant: str, body: dict) -> int:
        try:
            r = self._http.post(
                self._url(tenant, "/cancel"),
                headers={"Authorization": f"Bearer {self._tokens.token()}"},
                json=body,
                timeout=TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise RegistryError(f"cancel by release: {type(exc).__name__}") from None
        if r.status_code // 100 != 2:
            raise RegistryError(f"cancel by release answered {r.status_code}", status=r.status_code)
        try:
            body = r.json()
            cancelled, failed = list(body["cancelled"]), list(body.get("failed") or [])
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RegistryError("cancel by release: the answer is unusable") from None
        if failed:
            raise RegistryError(
                f"cancel by release: {len(failed)} session(s) not cancelled, "
                f"{len(cancelled)} cancelled"
            )
        return len(cancelled)

    def list_running(self, tenant: str, agent: str) -> list[dict]:
        try:
            r = self._http.get(
                self._url(tenant, ""),
                params={"state": "running", "agent": agent},
                headers={"Authorization": f"Bearer {self._tokens.token()}"},
                timeout=TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise RegistryError(f"list sessions: {type(exc).__name__}") from None
        if r.status_code // 100 != 2:
            raise RegistryError(f"list sessions answered {r.status_code}", status=r.status_code)
        try:
            rows = list(r.json()["sessions"])
            if not all(isinstance(row, dict) for row in rows):
                raise TypeError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RegistryError("list sessions: the answer is unusable") from None
        return rows
