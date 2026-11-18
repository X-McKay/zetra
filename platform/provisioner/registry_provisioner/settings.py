"""Settings, read once from the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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
    namespace: str
    interval_seconds: float = 30.0
    token_url: str | None = None
    client_id: str = ""
    token_path: str | None = None
    ca_file: str | None = None
    heartbeat_file: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        def get(name: str) -> str | None:
            return env.get(f"PROVISIONER_{name}") or None

        required = ("CLUSTER", "REGISTRY_URL", "NAMESPACE")
        missing = [f"PROVISIONER_{n}" for n in required if not get(n)]
        if missing:
            raise SettingsError(f"not set: {', '.join(missing)}")
        cluster = get("CLUSTER")
        interval = get("INTERVAL_SECONDS") or "30"
        try:
            seconds = float(interval)
        except ValueError:
            raise SettingsError("PROVISIONER_INTERVAL_SECONDS is not a number") from None
        if seconds <= 0:
            raise SettingsError("PROVISIONER_INTERVAL_SECONDS must be above zero")
        for name in ("REGISTRY_URL", "TOKEN_URL"):
            check_url(f"PROVISIONER_{name}", get(name))
        return cls(
            cluster=cluster,
            registry_url=get("REGISTRY_URL").rstrip("/"),
            namespace=get("NAMESPACE"),
            interval_seconds=seconds,
            token_url=get("TOKEN_URL"),
            client_id=get("CLIENT_ID") or f"registry-provisioner-{cluster}",
            token_path=get("TOKEN_PATH"),
            ca_file=get("CA_FILE"),
            heartbeat_file=get("HEARTBEAT_FILE"),
        )
