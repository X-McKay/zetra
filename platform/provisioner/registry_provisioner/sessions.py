"""The tenant's session interface: cancel the sessions of a revoked release.

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


class HttpSessions:
    def __init__(self, template: str, registry: HttpRegistry) -> None:
        self._template = template
        self._tokens = registry.tokens
        self._http = registry.http

    def cancel_release(self, tenant: str, agent: str, release: str, reason: str) -> int:
        base = self._template.replace("{tenant}", quote(tenant, safe="")).rstrip("/")
        path = f"/v1/tenants/{quote(tenant, safe='')}/sessions/cancel"
        try:
            r = self._http.post(
                base + path,
                headers={"Authorization": f"Bearer {self._tokens.token()}"},
                json={"agent": agent, "release": release, "reason": reason},
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
