"""The sandbox pod of section 5.2, rendered from a release, its Agent and the platform config.

`render_pod_template` produces the per-release template the controller stores in
ConfigMap `podtemplate-<release>`; per-session fields are left empty. The Session Runner
fills them with `instantiate` and changes nothing else.
"""

from __future__ import annotations

import copy
import re
from decimal import Decimal
from typing import Any

from agent_platform import naming
from agent_platform.constants import (
    AGENT_COMMAND,
    AGENT_TOKEN_PATH,
    ANNOTATION_NETPOL_ATTESTED,
    EGRESS_GUARD_TIMEOUT_SECONDS,
    GATEWAY_CA_MOUNT,
    GATEWAY_CA_PATH,
    GATEWAY_PORT,
    INFERENCE_HOST,
    KATA_RUNTIME_CLASS,
    LABEL_AGENT,
    LABEL_GENERATION,
    LABEL_RELEASE,
    LABEL_SANDBOX,
    LABEL_SESSION,
    LABEL_TENANT,
    ANNOTATION_POLICY_DIGEST,
    MCP_HOST,
    PROJECTED_TOKEN_SECONDS,
    SANDBOX_UID,
    TOKEN_HOST,
    TOKEN_ROUTE_PATH,
    PlatformConfig,
)

DEFAULT_CPU_LIMIT = "1"
DEFAULT_MEMORY_LIMIT = "2Gi"
CPU_REQUEST = "500m"
MEMORY_REQUEST = "1Gi"

_SUFFIX = {
    "": 1,
    "m": Decimal("0.001"),
    "k": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
}
_QUANTITY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(m|k|M|G|T|Ki|Mi|Gi|Ti)?$")


def quantity(value: str) -> Decimal:
    match = _QUANTITY.fullmatch(str(value))
    if not match:
        raise ValueError(f"unsupported resource quantity {value!r}")
    return Decimal(match.group(1)) * _SUFFIX[match.group(2) or ""]


def _request(default: str, limit: str) -> str:
    # The template's requests, never above the Agent's limit.
    return default if quantity(default) <= quantity(limit) else limit


def render_pod_template(
    agent: dict[str, Any],
    release: dict[str, Any],
    bundle: dict[str, Any],
    policy_digest: str,
    cfg: PlatformConfig,
) -> dict[str, Any]:
    name = agent["metadata"]["name"]
    tenant = naming.require_tenant(agent["metadata"]["namespace"])
    release_name = release["metadata"]["name"]
    runtime = agent["spec"].get("runtime") or {}
    cpu = str(runtime.get("cpu", DEFAULT_CPU_LIMIT))
    memory = str(runtime.get("memory", DEFAULT_MEMORY_LIMIT))
    gateway = f"{cfg.gateway_cluster_ip}:{GATEWAY_PORT}"

    env = [
        {"name": "AGENT_GATEWAY_URL", "value": f"https://{gateway}"},
        {"name": "AGENT_GATEWAY_CA", "value": GATEWAY_CA_PATH},
        {"name": "AGENT_TOKEN_HOST", "value": TOKEN_HOST},
        {"name": "AGENT_INFERENCE_HOST", "value": INFERENCE_HOST},
        {"name": "AGENT_MCP_HOST", "value": MCP_HOST},
        {"name": "AGENT_CLIENT_ID", "value": bundle["client_id"]},
        {"name": "AGENT_TOKEN_PATH", "value": AGENT_TOKEN_PATH},
        {"name": "AGENT_TOKEN_ENDPOINT_PATH", "value": TOKEN_ROUTE_PATH},
    ]
    if bundle["models"]:
        env.append({"name": "AGENT_MODEL_ROUTE", "value": f"/models/{min(bundle['models'])}"})
    env += [
        {"name": "AGENT_TOKENS_PER_SESSION", "value": str(bundle["budgets"]["tokens_per_session"])},
        {"name": "AGENT_SESSION_ID", "value": ""},
    ]

    hardened = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "",
            "namespace": tenant,
            "labels": {
                LABEL_SANDBOX: "true",
                LABEL_AGENT: name,
                LABEL_TENANT: tenant,
                LABEL_RELEASE: release_name,
                LABEL_SESSION: "",
                LABEL_GENERATION: "",
            },
            "annotations": {ANNOTATION_POLICY_DIGEST: policy_digest},
        },
        "spec": {
            "runtimeClassName": KATA_RUNTIME_CLASS,
            "serviceAccountName": naming.service_account(name),
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "hostNetwork": False,
            "hostPID": False,
            "hostIPC": False,
            "dnsPolicy": "None",
            "dnsConfig": {"nameservers": ["127.0.0.1"]},
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 30,
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": SANDBOX_UID,
                "runAsGroup": SANDBOX_UID,
                "fsGroup": SANDBOX_UID,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "initContainers": [
                {
                    "name": "egress-guard",
                    "image": cfg.egress_guard_image,
                    "args": [
                        "--gateway",
                        gateway,
                        "--forbidden",
                        ",".join(cfg.forbidden),
                        "--attestation",
                        "/etc/podinfo/annotations",
                        "--attestation-key",
                        ANNOTATION_NETPOL_ATTESTED,
                        "--timeout",
                        str(EGRESS_GUARD_TIMEOUT_SECONDS),
                    ],
                    "securityContext": copy.deepcopy(hardened),
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "32Mi"},
                        "limits": {"cpu": "100m", "memory": "64Mi"},
                    },
                    "volumeMounts": [
                        {"name": "podinfo", "mountPath": "/etc/podinfo", "readOnly": True},
                    ],
                }
            ],
            "containers": [
                {
                    "name": "agent",
                    "image": release["spec"]["image"],
                    "command": list(AGENT_COMMAND),
                    "env": env,
                    "resources": {
                        "requests": {
                            "cpu": _request(CPU_REQUEST, cpu),
                            "memory": _request(MEMORY_REQUEST, memory),
                        },
                        "limits": {"cpu": cpu, "memory": memory},
                    },
                    # privileged is explicit so Kyverno's `privileged: "!true"` pin (6.5
                    # Rule 2) never depends on how a pattern treats an absent field.
                    "securityContext": dict(copy.deepcopy(hardened), privileged=False),
                    "volumeMounts": [
                        {
                            "name": "agent-identity",
                            "mountPath": "/var/run/secrets/agent-identity",
                            "readOnly": True,
                        },
                        {"name": "gateway-ca", "mountPath": GATEWAY_CA_MOUNT, "readOnly": True},
                        {"name": "workspace", "mountPath": "/sandbox"},
                        {"name": "tmp", "mountPath": "/tmp"},
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "agent-identity",
                    "projected": {
                        "defaultMode": 0o440,
                        "sources": [
                            {
                                "serviceAccountToken": {
                                    "audience": cfg.issuer_url,
                                    "expirationSeconds": PROJECTED_TOKEN_SECONDS,
                                    "path": "token",
                                }
                            }
                        ],
                    },
                },
                {
                    "name": "gateway-ca",
                    "configMap": {"name": cfg.ca_configmap},
                },
                {
                    "name": "podinfo",
                    "downwardAPI": {
                        "items": [
                            {
                                "path": "annotations",
                                "fieldRef": {"fieldPath": "metadata.annotations"},
                            }
                        ]
                    },
                },
                {"name": "workspace", "emptyDir": {"sizeLimit": "2Gi"}},
                {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
            ],
        },
    }


def instantiate(template: dict[str, Any], session_id: str, generation: int) -> dict[str, Any]:
    """Fill the fields marked `# per session` in 5.2; everything else is unchanged."""
    naming.require_session_id(session_id)
    pod = copy.deepcopy(template)
    pod["metadata"]["name"] = naming.sandbox_name(session_id, generation)
    pod["metadata"]["labels"][LABEL_SESSION] = session_id
    pod["metadata"]["labels"][LABEL_GENERATION] = str(generation)
    for container in pod["spec"]["containers"]:
        for var in container.get("env", []):
            if var["name"] == "AGENT_SESSION_ID":
                var["value"] = session_id
    return pod
