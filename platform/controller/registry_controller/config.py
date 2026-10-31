"""Controller configuration, read once from the environment."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_platform.constants import PLATFORM_NAMESPACE, PlatformConfig


@dataclass(frozen=True)
class ControllerConfig:
    platform: PlatformConfig
    idp_alias: str  # kubernetes-<cluster>, the realm's identity provider entry (7.2)
    ca_source_namespace: str = PLATFORM_NAMESPACE
    keycloak_url: str = ""  # https://idp.<domain>; empty disables nothing, see operator
    keycloak_realm: str = "agents"
    keycloak_client_id: str = "registry-controller"
    keycloak_secret_file: str = "/var/run/secrets/idp-client/client_secret"
    keycloak_client_template: str = ""  # path to the exported hand-made client JSON
    cosign_key: str = "/etc/cosign/cosign.pub"
    signature_profile: str = "homelab"  # homelab | enterprise (cosign key) | keyless
    trusted_root: str = "/etc/sigstore/trusted_root.json"  # keyless: private Sigstore root
    rekor_url: str = ""  # keyless: https://rekor.<domain>; empty uses the root's log only
    resync_seconds: float = 60.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ControllerConfig:
        e = os.environ if env is None else env
        return cls(
            platform=PlatformConfig.from_env(e),
            idp_alias=e["REGISTRY_IDP_ALIAS"],
            ca_source_namespace=e.get("REGISTRY_CA_SOURCE_NAMESPACE", PLATFORM_NAMESPACE),
            keycloak_url=e["REGISTRY_KEYCLOAK_URL"],
            keycloak_realm=e.get("REGISTRY_KEYCLOAK_REALM", "agents"),
            keycloak_client_id=e.get("REGISTRY_KEYCLOAK_CLIENT_ID", "registry-controller"),
            keycloak_secret_file=e.get("REGISTRY_KEYCLOAK_SECRET_FILE", cls.keycloak_secret_file),
            keycloak_client_template=e.get("REGISTRY_KEYCLOAK_CLIENT_TEMPLATE", ""),
            cosign_key=e.get("REGISTRY_COSIGN_KEY", cls.cosign_key),
            signature_profile=e.get("REGISTRY_SIGNATURE_PROFILE", "homelab"),
            trusted_root=e.get("REGISTRY_SIGSTORE_TRUSTED_ROOT", cls.trusted_root),
            rekor_url=e.get("REGISTRY_SIGSTORE_REKOR_URL", ""),
            resync_seconds=float(e.get("REGISTRY_RESYNC_SECONDS", "60")),
        )

    def client_template(self) -> dict[str, Any] | None:
        if not self.keycloak_client_template:
            return None
        return json.loads(Path(self.keycloak_client_template).read_text())
