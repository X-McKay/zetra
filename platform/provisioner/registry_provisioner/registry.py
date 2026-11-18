"""The registry port, and its httpx implementation with the Keycloak client assertion token.

No token is logged or put in an error message.
"""

from __future__ import annotations

import logging
import ssl
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from .settings import Settings

log = logging.getLogger("registry_provisioner")

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
REFRESH_BEFORE = 60.0  # seconds
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
DESIRED_KEYS = ("mode", "generation", "fieldManager", "namespaces", "fluxInventories")


class RegistryError(Exception):
    """The registry cannot be reached, or answered with an error."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


class RegistryPort(Protocol):
    def desired(self, last_generation: int) -> dict: ...

    def report(self, observations: dict) -> dict: ...


class TokenSource:
    """An access token from the realm, got by presenting the projected service-account token."""

    def __init__(
        self,
        http: httpx.Client,
        token_url: str,
        client_id: str,
        token_path: str,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._url = token_url
        self._client_id = client_id
        self._path = token_path
        self._clock = clock
        self._token: str | None = None
        self._expires = 0.0

    def token(self) -> str:
        if self._token is None or self._clock() >= self._expires - REFRESH_BEFORE:
            self._refresh()
        assert self._token is not None
        return self._token

    def invalidate(self) -> None:
        self._token = None

    def _refresh(self) -> None:
        try:  # the kubelet rotates the projected token, so read it every time
            with open(self._path, encoding="utf-8") as f:
                assertion = f.read().strip()
        except OSError as exc:
            raise RegistryError(
                f"cannot read the service account token file: {type(exc).__name__}"
            ) from None
        started = self._clock()
        try:
            r = self._http.post(
                self._url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_assertion_type": ASSERTION_TYPE,
                    "client_assertion": assertion,
                },
            )
        except httpx.HTTPError as exc:
            raise RegistryError(f"token endpoint unreachable: {type(exc).__name__}") from None
        if r.status_code != 200:
            try:
                error = str(r.json().get("error", "?"))[:64]
            except (ValueError, AttributeError):
                error = "?"
            raise RegistryError(f"token endpoint answered {r.status_code}: {error}")
        try:
            body = r.json()
            token = body["access_token"]
            lifetime = float(body.get("expires_in", 300))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RegistryError("token endpoint answered with an unusable body") from None
        if not isinstance(token, str) or not token:
            raise RegistryError("token endpoint answered with an unusable body")
        self._token, self._expires = token, started + lifetime


class HttpRegistry:
    def __init__(
        self,
        base_url: str,
        cluster: str,
        tokens: TokenSource,
        http: httpx.Client,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._cluster = quote(cluster, safe="")
        self._tokens = tokens
        self._http = http

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx.BaseTransport | None = None
    ) -> HttpRegistry:
        from .settings import SettingsError

        if not settings.token_url or not settings.token_path:
            raise SettingsError("PROVISIONER_TOKEN_URL and PROVISIONER_TOKEN_PATH are required")
        verify = (
            ssl.create_default_context(cafile=settings.ca_file)
            if settings.ca_file
            else ssl.create_default_context()
        )
        http = httpx.Client(
            verify=verify, timeout=TIMEOUT, follow_redirects=False, transport=transport
        )
        tokens = TokenSource(http, settings.token_url, settings.client_id, settings.token_path)
        return cls(settings.registry_url, settings.cluster, tokens, http)

    def desired(self, last_generation: int) -> dict:
        path = f"/v1/clusters/{self._cluster}/desired"
        answer = self._call("GET", path, params={"lastGeneration": last_generation})
        missing = [k for k in DESIRED_KEYS if k not in answer]
        if missing:
            raise RegistryError(f"the desired answer lacks {', '.join(missing)}")
        return answer

    def report(self, observations: dict) -> dict:
        return self._call("POST", f"/v1/clusters/{self._cluster}/observations", json=observations)

    def _call(self, method: str, path: str, **kwargs: Any) -> dict:
        for attempt in (0, 1):
            token = self._tokens.token()
            try:
                r = self._http.request(
                    method,
                    self._base + path,
                    headers={"Authorization": f"Bearer {token}"},
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                raise RegistryError(f"{method} {path}: {type(exc).__name__}") from None
            log.debug("%s %s answered %s", method, path, r.status_code)
            if r.status_code == 401 and attempt == 0:
                self._tokens.invalidate()
                continue
            break
        if r.status_code // 100 != 2:
            raise self._error(method, path, r)
        try:
            body = r.json()
        except ValueError:
            raise RegistryError(f"{method} {path}: the answer is not JSON") from None
        if not isinstance(body, dict):
            raise RegistryError(f"{method} {path}: the answer is not an object")
        return body

    @staticmethod
    def _error(method: str, path: str, r: httpx.Response) -> RegistryError:
        code, message, retryable = None, "", False
        try:
            err = r.json()["error"]
            code, message = str(err["code"]), str(err.get("message", ""))[:300]
            retryable = bool(err.get("retryable", False))
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        text = f"{method} {path} answered {r.status_code}"
        if code:
            text += f" {code}: {message}"
        return RegistryError(text, status=r.status_code, code=code, retryable=retryable)
