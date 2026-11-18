"""The session interface port (platform/session-api/contract/sessions.md) and its httpx client."""

from __future__ import annotations

from typing import Protocol
from urllib.parse import quote

import httpx

from .registry import ApiError, TokenSource, call
from .settings import Settings


class SessionError(ApiError):
    """The session interface cannot be reached, or answered with an error."""


class SessionsPort(Protocol):
    def start(self, tenant: str, body: dict) -> dict: ...

    def read(self, tenant: str, agent: str, session_id: str) -> dict: ...

    def cancel(self, tenant: str, agent: str, session_id: str, reason: str) -> dict: ...


class HttpSessions:
    def __init__(self, template: str, tokens: TokenSource, http: httpx.Client) -> None:
        self._template = template
        self._tokens = tokens
        self._http = http

    @classmethod
    def from_settings(
        cls, settings: Settings, http: httpx.Client, tokens: TokenSource
    ) -> HttpSessions:
        return cls(settings.session_api_url, tokens, http)

    def _base(self, tenant: str) -> str:
        host = self._template.replace("{tenant}", quote(tenant, safe=""))
        return f"{host}/v1/tenants/{quote(tenant, safe='')}/sessions"

    def start(self, tenant: str, body: dict) -> dict:
        return self._call("POST", self._base(tenant), json=body)

    def read(self, tenant: str, agent: str, session_id: str) -> dict:
        url = f"{self._base(tenant)}/{quote(agent, safe='')}/{quote(session_id, safe='')}"
        return self._call("GET", url)

    def cancel(self, tenant: str, agent: str, session_id: str, reason: str) -> dict:
        url = f"{self._base(tenant)}/{quote(agent, safe='')}/{quote(session_id, safe='')}/cancel"
        return self._call("POST", url, json={"reason": reason})

    def _call(self, method: str, url: str, **kwargs) -> dict:
        return call(self._http, self._tokens, SessionError, method, url, **kwargs)
