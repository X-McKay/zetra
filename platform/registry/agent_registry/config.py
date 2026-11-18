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
EVALUATOR = "registry-evaluator"


def _whole(env: Mapping[str, str], name: str, default: int) -> int:
    """A whole number of at least zero from the environment; the default when unset or empty."""
    text = env.get(name, "")
    if text == "":
        return default
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f"{name} is not a whole number: {text!r}") from None
    if value < 0:
        raise ValueError(f"{name} is negative")
    return value


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
    # The release report (contract/delivery.md section 3.2). Empty audience: reports are off.
    github_issuer: str = "https://token.actions.githubusercontent.com"
    github_jwks_url: str = ""  # default: <github_issuer>/.well-known/jwks
    report_audience: str = ""
    # How long after it is stored a security revocation is offered to the provisioner, which
    # cancels the release's sessions (contract/delivery.md section 8.3).
    revocation_cancel_seconds: int = 86400
    # How long a move's agents are evaluated on the new model before those still held are
    # suspended (contract/dependencies.md section 6.3). A request may ask for 60 to 86400.
    alias_move_evaluation_seconds: int = 1800
    host: str = "0.0.0.0"  # noqa: S104 - a container port behind a NetworkPolicy
    port: int = 8080

    @property
    def jwks(self) -> str:
        return self.jwks_url or self.issuer.rstrip("/") + "/protocol/openid-connect/certs"

    @property
    def github_jwks(self) -> str:
        return self.github_jwks_url or self.github_issuer.rstrip("/") + "/.well-known/jwks"

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
            github_issuer=env.get("REGISTRY_GITHUB_ISSUER", default.github_issuer),
            github_jwks_url=env.get("REGISTRY_GITHUB_JWKS_URL", ""),
            report_audience=env.get("REGISTRY_REPORT_AUDIENCE", ""),
            revocation_cancel_seconds=_whole(
                env, "REGISTRY_REVOCATION_CANCEL_SECONDS", default.revocation_cancel_seconds
            ),
            alias_move_evaluation_seconds=_whole(
                env,
                "REGISTRY_ALIAS_MOVE_EVALUATION_SECONDS",
                default.alias_move_evaluation_seconds,
            ),
            host=env.get("REGISTRY_HOST", default.host),
            port=int(env.get("REGISTRY_PORT", default.port)),
        )
