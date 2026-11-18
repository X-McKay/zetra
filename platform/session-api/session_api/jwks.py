"""The token check against the realm's published keys. Copied from the registry's, not imported."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt

from session_api.auth import AuthError, Principal
from session_api.config import Settings

ALGORITHMS = ["RS256", "ES256"]
LEEWAY_SECONDS = 10
# Unknown key ids cause at most one fetch in this many seconds, whatever arrives.
REFETCH_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 5.0


class JwksVerifier:
    """`TokenVerifier` for tokens of `settings.issuer` with audience `settings.audience`.

    Messages of `AuthError` are fixed text: nothing of the token is ever put in one.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        fetch: Callable[[], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings
        self._fetch = fetch or self._fetch_over_http
        self._clock = clock
        self._keys: dict[str, tuple[Any, str | None]] = {}  # kid -> (key, the key's own alg)
        self._fetched_at: float | None = None
        self._lock = threading.Lock()

    def _fetch_over_http(self) -> dict[str, Any]:
        answer = httpx.get(self._settings.jwks, timeout=FETCH_TIMEOUT_SECONDS)
        answer.raise_for_status()
        return answer.json()

    def _refresh(self) -> None:
        """Replace the cached keys, unless the last attempt was less than 30 seconds ago. The
        attempt counts whether or not it worked, so a down realm is not asked more often."""
        now = self._clock()
        if self._fetched_at is not None and now - self._fetched_at < REFETCH_SECONDS:
            return
        self._fetched_at = now
        try:
            listed = self._fetch()["keys"]
            if not isinstance(listed, list):
                raise TypeError("keys is not a list")
        except Exception:  # noqa: BLE001 - any failure is the same answer to the caller
            raise AuthError("the realm's published keys could not be fetched") from None
        keys: dict[str, tuple[Any, str | None]] = {}
        for entry in listed:
            try:
                if entry.get("use", "sig") != "sig" or not isinstance(entry.get("kid"), str):
                    continue
                keys[entry["kid"]] = (jwt.PyJWK(entry).key, entry.get("alg"))
            except Exception:  # noqa: BLE001, S112 - a key this service cannot use is skipped
                continue
        self._keys = keys

    def _key(self, kid: str, alg: str) -> Any:
        found = self._keys.get(kid)
        if found is None:
            with self._lock:
                found = self._keys.get(kid)  # another request may have just fetched it
                if found is None:
                    self._refresh()
                    found = self._keys.get(kid)
        if found is None:
            raise AuthError("no published key matches the token")
        key, key_alg = found
        if key_alg is not None and key_alg != alg:
            raise AuthError("the token's algorithm does not match its key")
        return key

    def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthError("the token is not a well-formed JWT") from None
        alg, kid = header.get("alg"), header.get("kid")
        if alg not in ALGORITHMS:
            raise AuthError("the token's signing algorithm is not accepted")
        if not isinstance(kid, str) or not kid:
            raise AuthError("the token names no signing key")
        key = self._key(kid, alg)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                issuer=self._settings.issuer,
                audience=self._settings.audience,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise AuthError("the token has expired") from None
        except jwt.InvalidAudienceError:
            raise AuthError("the token is not for this service") from None
        except jwt.InvalidIssuerError:
            raise AuthError("the token is from another issuer") from None
        except jwt.InvalidSignatureError:
            raise AuthError("the token's signature does not verify") from None
        except jwt.PyJWTError:
            raise AuthError("the token is not valid") from None
        if claims.get("typ") != "Bearer":
            raise AuthError("the token is not an access token")
        return self._principal(claims)

    @staticmethod
    def _principal(claims: dict[str, Any]) -> Principal:
        def text(name: str) -> str:
            value = claims.get(name)
            return value if isinstance(value, str) else ""

        username = text("preferred_username")
        roles = (claims.get("realm_access") or {}).get("roles")
        service = "client_id" in claims or username.startswith("service-account-")
        azp = text("azp")
        if service and not username:
            # A client whose scopes leave out `profile` has no preferred_username; events
            # then name the client, not its service account's opaque id.
            username = text("client_id") or azp
        return Principal(
            subject=claims["sub"],
            username=username,
            roles=frozenset(r for r in roles if isinstance(r, str))
            if isinstance(roles, list)
            else frozenset(),
            person=not service,
            email=None if service else (text("email") or None),
            azp=azp,
        )
