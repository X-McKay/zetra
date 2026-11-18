"""Who is calling, and which of the three callers they are (`contract/sessions.md` section 2).

A token is checked by a `TokenVerifier`; `jwks.py` holds the real one, `StaticVerifier` is for
tests. Roles are realm roles. A caller is one of:

- operator: role `registry-operator`, a person with an email address;
- evaluator: role `registry-evaluator`, a service account whose `azp` is
  `registry-evaluator-<cluster>`;
- provisioner: role `registry-provisioner`, a service account whose `azp` is
  `registry-provisioner-<cluster>`.

Anything else, a reader or another cluster's client included, is none of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from session_api.config import EVALUATOR, OPERATOR, PROVISIONER, Settings

KINDS = ("operator", "evaluator", "provisioner")


@dataclass(frozen=True)
class Principal:
    subject: str  # the token's `sub`
    username: str  # `preferred_username`
    roles: frozenset[str]
    person: bool  # False for a client's service account
    email: str | None = None
    azp: str = ""  # the client the token was issued to

    @property
    def actor(self) -> str:
        """The name written into memos and records."""
        return self.email or self.username or self.subject


class AuthError(Exception):
    """The token is missing, malformed, expired, or not from this issuer for this audience."""


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal:
        """Return the caller, or raise `AuthError`. Never returns a caller for a bad token."""
        ...


class StaticVerifier:
    """Tokens are dictionary keys. For tests only."""

    def __init__(self, tokens: dict[str, Principal]):
        self._tokens = dict(tokens)

    def verify(self, token: str) -> Principal:
        try:
            return self._tokens[token]
        except KeyError:
            raise AuthError("unknown token") from None


def kinds(principal: Principal, settings: Settings) -> list[str]:
    """Which of the three callers this token is, in `KINDS` order."""
    found = []
    if OPERATOR in principal.roles and principal.person and principal.email:
        found.append("operator")
    if (
        EVALUATOR in principal.roles
        and not principal.person
        and principal.azp == f"registry-evaluator-{settings.cluster}"
    ):
        found.append("evaluator")
    if (
        PROVISIONER in principal.roles
        and not principal.person
        and principal.azp == f"registry-provisioner-{settings.cluster}"
    ):
        found.append("provisioner")
    return found
