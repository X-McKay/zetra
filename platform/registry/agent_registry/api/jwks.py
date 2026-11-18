"""The token check against the realm's published keys (package 1.3)."""

from __future__ import annotations

from agent_registry.api.auth import Principal
from agent_registry.config import Settings


class JwksVerifier:
    """`TokenVerifier` for tokens of `settings.issuer` with audience `settings.audience`."""

    def __init__(self, settings: Settings):
        raise NotImplementedError

    def verify(self, token: str) -> Principal:
        raise NotImplementedError
