"""Settings, read from the environment once at start."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from runner.types import TENANT_RE, WORKER_POOLS

ANNOTATION_PREFIX = "registry.agentplatform.io/"
DEPLOYMENT_ANNOTATION = ANNOTATION_PREFIX + "deployment"
CURRENT_RELEASE_ANNOTATION = ANNOTATION_PREFIX + "current-release"

OPERATOR = "registry-operator"
EVALUATOR = "registry-evaluator"
PROVISIONER = "registry-provisioner"


@dataclass(frozen=True)
class Settings:
    tenant: str
    cluster: str
    artifact_root: str
    issuer: str = "https://idp.almckay.io/realms/agents"
    audience: str = "agent-registry"
    jwks_url: str = ""  # default: <issuer>/protocol/openid-connect/certs
    worker_pool: str = "legacy"
    host: str = "0.0.0.0"  # noqa: S104 - a container port behind a NetworkPolicy
    port: int = 8080

    @property
    def jwks(self) -> str:
        return self.jwks_url or self.issuer.rstrip("/") + "/protocol/openid-connect/certs"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Raises ValueError, naming the variable, when a required one is missing or invalid."""
        env = os.environ if env is None else env
        for name in ("SESSION_API_TENANT", "SESSION_API_CLUSTER", "SESSION_API_ARTIFACT_ROOT"):
            if not env.get(name):
                raise ValueError(f"{name} is not set")
        if not TENANT_RE.fullmatch(env["SESSION_API_TENANT"]):
            raise ValueError("SESSION_API_TENANT is not a tenant name")
        pool = env.get("SESSION_API_WORKER_POOL", cls.worker_pool)
        if pool not in WORKER_POOLS:
            raise ValueError("SESSION_API_WORKER_POOL is not a known pool")
        return cls(
            tenant=env["SESSION_API_TENANT"],
            cluster=env["SESSION_API_CLUSTER"],
            artifact_root=env["SESSION_API_ARTIFACT_ROOT"],
            issuer=env.get("SESSION_API_ISSUER", cls.issuer),
            audience=env.get("SESSION_API_AUDIENCE", cls.audience),
            jwks_url=env.get("SESSION_API_JWKS_URL", ""),
            worker_pool=pool,
            host=env.get("SESSION_API_HOST", cls.host),
            port=int(env.get("SESSION_API_PORT", cls.port)),
        )
