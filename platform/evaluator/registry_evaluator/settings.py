"""Settings, read once from the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from socket import gethostname
from urllib.parse import urlsplit


class SettingsError(Exception):
    pass


def check_url(name: str, url: str | None) -> None:
    """https anywhere; plain http only to a service name inside the cluster."""
    if not url:
        return
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        raise SettingsError(f"{name} is not a URL") from None
    if parts.scheme == "https" and host:
        return
    in_cluster = host.endswith((".svc", ".svc.cluster.local")) and host not in (
        ".svc",
        ".svc.cluster.local",
    )
    if parts.scheme == "http" and in_cluster:
        return
    raise SettingsError(
        f"{name} must be https, or http to a host ending in .svc or .svc.cluster.local: "
        "tokens do not travel in clear outside the cluster"
    )


@dataclass(frozen=True)
class Settings:
    cluster: str
    registry_url: str
    session_api_url: str  # a template containing {tenant}
    name: str
    poll_seconds: float = 15.0
    token_url: str | None = None
    client_id: str = ""
    token_path: str | None = None
    ca_file: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        def get(name: str) -> str | None:
            return env.get(f"EVALUATOR_{name}") or None

        required = ("CLUSTER", "REGISTRY_URL", "SESSION_API_URL")
        missing = [f"EVALUATOR_{n}" for n in required if not get(n)]
        if missing:
            raise SettingsError(f"not set: {', '.join(missing)}")
        cluster = get("CLUSTER")
        poll = get("POLL_SECONDS") or "15"
        try:
            seconds = float(poll)
        except ValueError:
            raise SettingsError("EVALUATOR_POLL_SECONDS is not a number") from None
        if seconds <= 0:
            raise SettingsError("EVALUATOR_POLL_SECONDS must be above zero")
        template = get("SESSION_API_URL")
        if "{tenant}" not in template:
            raise SettingsError("EVALUATOR_SESSION_API_URL must contain {tenant}")
        for name, url in (
            ("REGISTRY_URL", get("REGISTRY_URL")),
            ("SESSION_API_URL", template.replace("{tenant}", "tenant")),
            ("TOKEN_URL", get("TOKEN_URL")),
        ):
            check_url(f"EVALUATOR_{name}", url)
        return cls(
            cluster=cluster,
            registry_url=get("REGISTRY_URL").rstrip("/"),
            session_api_url=template.rstrip("/"),
            name=get("NAME") or gethostname(),
            poll_seconds=seconds,
            token_url=get("TOKEN_URL"),
            client_id=get("CLIENT_ID") or f"registry-evaluator-{cluster}",
            token_path=get("TOKEN_PATH"),
            ca_file=get("CA_FILE"),
        )
