"""Who is calling, and what each group of routes needs from them.

The service trusts one issuer (the `agents` realm). A token is checked by a `TokenVerifier`;
`agent_registry/api/jwks.py` holds the real one, `StaticVerifier` is for tests.

Roles are realm roles (`realm_access.roles`):

- `registry-reader`: the read routes.
- `registry-operator`: the write routes. The caller must be a person (not a client's service
  account) with an email address, because the address is written as `approvedBy`.
- `registry-provisioner`: the cluster routes, and only for the cluster the client is named
  for: the token's `azp` is `registry-provisioner-<cluster>`.
- `registry-evaluator`: the evaluation routes (`contract/delivery.md` section 6.2), checked the
  same way: the token's `azp` is `registry-evaluator-<cluster>`.

An operator may also read. A provisioner may not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from fastapi import Request

from agent_registry.api.errors import ApiError
from agent_registry.config import EVALUATOR, OPERATOR, PROVISIONER, READER

PROVISIONER_CLIENT_PREFIX = "registry-provisioner-"
EVALUATOR_CLIENT_PREFIX = "registry-evaluator-"


@dataclass(frozen=True)
class Principal:
    subject: str  # the token's `sub`
    username: str  # `preferred_username`
    roles: frozenset[str]
    person: bool  # False for a client's service account
    email: str | None = None
    cluster: str | None = None  # set only for a provisioner client: the cluster in its name
    azp: str = ""  # the token's authorized party: the client the token was issued to

    @property
    def actor(self) -> str:
        """The name written into events."""
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


def _principal(request: Request) -> Principal:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError("not-signed-in", "a bearer token is required")
    try:
        return request.app.state.verifier.verify(token.strip())
    except AuthError as error:
        raise ApiError("not-signed-in", f"the token was not accepted: {error}") from None


def reader(request: Request) -> Principal:
    principal = _principal(request)
    if not principal.roles & {READER, OPERATOR}:
        raise ApiError.forbidden(f"the {READER} role is required")
    return principal


def operator(request: Request) -> Principal:
    principal = _principal(request)
    if OPERATOR not in principal.roles:
        raise ApiError.forbidden(f"the {OPERATOR} role is required")
    if not principal.person:
        raise ApiError.forbidden("this route is for a signed-in person, not a service account")
    if not principal.email:
        raise ApiError.forbidden("the token carries no email address to record")
    return principal


def provisioner(cluster: str, request: Request) -> Principal:
    principal = _principal(request)
    if PROVISIONER not in principal.roles:
        raise ApiError.forbidden(f"the {PROVISIONER} role is required")
    if principal.person or principal.cluster != cluster:
        raise ApiError.forbidden(f"this token is not the provisioner of cluster {cluster}")
    return principal


def evaluator_cluster(principal: Principal) -> str | None:
    """The cluster named by an evaluator client's `azp`, or None for any other caller."""
    if principal.person or not principal.azp.startswith(EVALUATOR_CLIENT_PREFIX):
        return None
    return principal.azp.removeprefix(EVALUATOR_CLIENT_PREFIX) or None


def evaluator_of_any_cluster(request: Request) -> Principal:
    """An evaluator of some cluster. Routes that name a deployment compare its cluster."""
    principal = _principal(request)
    if EVALUATOR not in principal.roles:
        raise ApiError.forbidden(f"the {EVALUATOR} role is required")
    if evaluator_cluster(principal) is None:
        raise ApiError.forbidden("this token is not an evaluator's service account")
    return principal


def evaluator(cluster: str, request: Request) -> Principal:
    principal = evaluator_of_any_cluster(request)
    if evaluator_cluster(principal) != cluster:
        raise ApiError.forbidden(f"this token is not the evaluator of cluster {cluster}")
    return principal
