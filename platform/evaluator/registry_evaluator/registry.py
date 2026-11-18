"""The token source, the shared HTTP call, and the registry port with its httpx implementation.

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

from .settings import Settings, SettingsError

log = logging.getLogger("registry_evaluator")

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
REFRESH_BEFORE = 60.0  # seconds
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
CLAIM_LEASE_SECONDS = 3600  # the suite is not known until the claim is answered


class ApiError(Exception):
    """A service cannot be reached (`status` is None), or answered with an error."""

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

    @property
    def transient(self) -> bool:
        """Worth trying again: no answer, or a server-side failure."""
        return self.status is None or self.status >= 500


class RegistryError(ApiError):
    """The registry cannot be reached, or answered with an error."""


class RegistryPort(Protocol):
    def claim(self, evaluator: str, lease_seconds: int) -> dict | None: ...

    def result(self, deployment: str, revision: int, attempt: int, body: dict) -> dict: ...


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
            raise ApiError(
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
            raise ApiError(f"token endpoint unreachable: {type(exc).__name__}") from None
        if r.status_code != 200:
            try:
                error = str(r.json().get("error", "?"))[:64]
            except (ValueError, AttributeError):
                error = "?"
            raise ApiError(f"token endpoint answered {r.status_code}: {error}")
        try:
            body = r.json()
            token = body["access_token"]
            lifetime = float(body.get("expires_in", 300))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ApiError("token endpoint answered with an unusable body") from None
        if not isinstance(token, str) or not token:
            raise ApiError("token endpoint answered with an unusable body")
        self._token, self._expires = token, started + lifetime


def make_http(settings: Settings, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    verify = (
        ssl.create_default_context(cafile=settings.ca_file)
        if settings.ca_file
        else ssl.create_default_context()
    )
    return httpx.Client(verify=verify, timeout=TIMEOUT, follow_redirects=False, transport=transport)


def make_tokens(settings: Settings, http: httpx.Client) -> TokenSource:
    if not settings.token_url or not settings.token_path:
        raise SettingsError("EVALUATOR_TOKEN_URL and EVALUATOR_TOKEN_PATH are required")
    return TokenSource(http, settings.token_url, settings.client_id, settings.token_path)


def call(
    http: httpx.Client,
    tokens: TokenSource,
    error_class: type[ApiError],
    method: str,
    url: str,
    **kwargs: Any,
) -> dict:
    """One authenticated JSON call; any answer but 2xx is raised as `error_class`."""
    where = f"{method} {url.split('://', 1)[-1].split('/', 1)[-1]}"
    for attempt in (0, 1):
        token = tokens.token()
        try:
            r = http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        except httpx.HTTPError as exc:
            raise error_class(f"{where}: {type(exc).__name__}") from None
        if r.status_code == 401 and attempt == 0:
            tokens.invalidate()
            continue
        break
    if r.status_code // 100 != 2:
        code, message, retryable = None, "", False
        try:
            err = r.json()["error"]
            code, message = str(err["code"]), str(err.get("message", ""))[:300]
            retryable = bool(err.get("retryable", False))
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        text = f"{where} answered {r.status_code}"
        if code:
            text += f" {code}: {message}"
        raise error_class(text, status=r.status_code, code=code, retryable=retryable) from None
    try:
        body = r.json()
    except ValueError:
        raise error_class(f"{where}: the answer is not JSON") from None
    if not isinstance(body, dict):
        raise error_class(f"{where}: the answer is not an object")
    return body


class HttpRegistry:
    def __init__(
        self, base_url: str, cluster: str, tokens: TokenSource, http: httpx.Client
    ) -> None:
        self._base = base_url.rstrip("/")
        self._cluster = quote(cluster, safe="")
        self._tokens = tokens
        self._http = http

    @classmethod
    def from_settings(
        cls, settings: Settings, http: httpx.Client, tokens: TokenSource
    ) -> HttpRegistry:
        return cls(settings.registry_url, settings.cluster, tokens, http)

    def claim(self, evaluator: str, lease_seconds: int) -> dict | None:
        path = f"/v1/clusters/{self._cluster}/evaluations/claim"
        answer = self._call(
            "POST", path, json={"evaluator": evaluator, "leaseSeconds": lease_seconds}
        )
        found = answer.get("evaluation")
        if found is None:
            return None
        if not isinstance(found, dict):
            raise RegistryError("the claim answer is not an evaluation")
        return found

    def result(self, deployment: str, revision: int, attempt: int, body: dict) -> dict:
        path = (
            f"/v1/deployments/{quote(deployment, safe='')}/revisions/{int(revision)}"
            f"/evaluations/{int(attempt)}/result"
        )
        return self._call("POST", path, json=body)

    def _call(self, method: str, path: str, **kwargs: Any) -> dict:
        return call(self._http, self._tokens, RegistryError, method, self._base + path, **kwargs)
