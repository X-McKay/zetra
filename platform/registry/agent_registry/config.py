"""Settings, read from the environment once at start."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

FIELD_MANAGER = "agent-registry-provisioner"
ANNOTATION_PREFIX = "registry.agentplatform.io/"

READER = "registry-reader"
OPERATOR = "registry-operator"
PROVISIONER = "registry-provisioner"


@dataclass(frozen=True)
class Settings:
    # PostgreSQL connection string. Comes from a Secret; never logged.
    dsn: str = ""
    issuer: str = "https://idp.almckay.io/realms/agents"
    audience: str = "agent-registry"
    jwks_url: str = ""  # default: <issuer>/protocol/openid-connect/certs
    # The catalog feed, mounted from a generated ConfigMap. Applied at start and on change.
    feed_path: str = ""
    # An observation older than this is "old": approval is refused on it, status says unknown.
    observation_max_age_seconds: int = 180
    # Start in recovery whatever the database says. Set on the first start after a restore.
    start_in_recovery: bool = False
    host: str = "0.0.0.0"  # noqa: S104 - a container port behind a NetworkPolicy
    port: int = 8080

    @property
    def jwks(self) -> str:
        return self.jwks_url or self.issuer.rstrip("/") + "/protocol/openid-connect/certs"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        default = cls()
        return cls(
            dsn=env.get("REGISTRY_DSN", ""),
            issuer=env.get("REGISTRY_ISSUER", default.issuer),
            audience=env.get("REGISTRY_AUDIENCE", default.audience),
            jwks_url=env.get("REGISTRY_JWKS_URL", ""),
            feed_path=env.get("REGISTRY_FEED_PATH", ""),
            observation_max_age_seconds=int(
                env.get("REGISTRY_OBSERVATION_MAX_AGE_SECONDS", default.observation_max_age_seconds)
            ),
            start_in_recovery=env.get("REGISTRY_START_IN_RECOVERY", "").lower() in ("1", "true"),
            host=env.get("REGISTRY_HOST", default.host),
            port=int(env.get("REGISTRY_PORT", default.port)),
        )
