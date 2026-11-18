"""The check of a GitHub Actions identity token (`contract/delivery.md` section 3.2).

Same shape as `api/jwks.py`: a protocol, the real verifier, and a static one for tests. The
real one reuses the key cache of `JwksVerifier`, pointed at GitHub's issuer and keys, with the
report audience. A token that the realm verifier accepts is not accepted here, and the other
way round, because each checks the signature against its own issuer's keys.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Protocol

import jwt

from agent_registry.api.auth import AuthError
from agent_registry.api.jwks import ALGORITHMS, LEEWAY_SECONDS, JwksVerifier
from agent_registry.config import Settings


@dataclass(frozen=True)
class GithubClaims:
    repository: str  # owner/name
    ref: str  # refs/tags/<agent>/v<version>
    ref_type: str
    sha: str
    job_workflow_ref: str


class GithubTokenVerifier(Protocol):
    def verify(self, token: str) -> GithubClaims:
        """Return the claims, or raise `AuthError`. Never returns for a bad token."""
        ...


class StaticGithubVerifier:
    """Tokens are dictionary keys. For tests only."""

    def __init__(self, tokens: dict[str, GithubClaims]):
        self._tokens = dict(tokens)

    def verify(self, token: str) -> GithubClaims:
        try:
            return self._tokens[token]
        except KeyError:
            raise AuthError("unknown token") from None


class GithubJwksVerifier(JwksVerifier):
    """Tokens of `settings.github_issuer` with audience `settings.report_audience`."""

    def __init__(self, settings: Settings, **how: Any):
        super().__init__(
            dataclasses.replace(
                settings,
                issuer=settings.github_issuer,
                audience=settings.report_audience,
                jwks_url=settings.github_jwks,
            ),
            **how,
        )

    def verify(self, token: str) -> GithubClaims:  # type: ignore[override]
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
        if not self._settings.audience:
            raise AuthError("reports are off")
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                issuer=self._settings.issuer,
                audience=self._settings.audience,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud"]},
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
        found = {
            name: claims.get(name)
            for name in ("repository", "ref", "ref_type", "sha", "job_workflow_ref")
        }
        if not all(isinstance(value, str) and value for value in found.values()):
            raise AuthError("the token lacks a claim of a GitHub Actions run")
        return GithubClaims(**found)
