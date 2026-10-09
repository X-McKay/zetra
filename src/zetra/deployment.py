"""Native Kubernetes candidates; never synthesize unverified vendor CRDs."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .analysis import risk
from .evidence import fingerprint, validate_evidence
from .manifest import ContractError, Manifest


def dns_label(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
        raise ContractError(f"Invalid Kubernetes namespace: {value}")
    return value


def immutable_image(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64}", value):
        raise ContractError(
            "Image must be an immutable repository@sha256:<64 lowercase hex> reference"
        )
    return value


def render(
    root: Path,
    manifest: Manifest,
    evidence: dict,
    *,
    image: str,
    namespace: str,
    gateway_namespace: str,
    gateway_label: str,
) -> list[dict]:
    validate_evidence(root, manifest, evidence)
    image = immutable_image(image)
    namespace, gateway_namespace = dns_label(namespace), dns_label(gateway_namespace)
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+=[a-zA-Z0-9_.-]+", gateway_label):
        raise ContractError("Gateway label must be key=value")
    key, value = gateway_label.split("=", 1)
    name = manifest.name
    labels = {"app.kubernetes.io/name": name, "zetra.dev/agent": name}
    meta = {"name": name, "namespace": namespace}
    container = {
        "name": "agent",
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "securityContext": {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "1", "memory": "512Mi"},
        },
        "env": [
            {"name": "ZETRA_AGENT_ROOT", "value": "/app/agent"},
            {"name": "ZETRA_MODE", "value": manifest.execution},
        ],
        "volumeMounts": [{"name": "scratch", "mountPath": "/tmp"}],
    }
    # The qualified image must provide its worker/service ENTRYPOINT. A one-shot
    # local example is intentionally not converted to a persistent service.
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            **meta,
            "labels": labels,
            "annotations": {
                "zetra.dev/start-status": "withheld-pending-live-qualification",
                "zetra.dev/source-sha256": fingerprint(root),
                "zetra.dev/evidence-trust": "local-unsigned-candidate",
                "zetra.dev/runtime-contract": "image must provide qualified persistent worker ENTRYPOINT",
            },
        },
        "spec": {
            "replicas": 0,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "serviceAccountName": name,
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 10001,
                        "fsGroup": 10001,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [container],
                    "volumes": [{"name": "scratch", "emptyDir": {"sizeLimit": "64Mi"}}],
                },
            },
        },
    }
    sa = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": meta,
        "automountServiceAccountToken": False,
    }
    deny = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {**meta, "name": name + "-deny"},
        "spec": {
            "podSelector": {"matchLabels": labels},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [],
        },
    }
    gateway = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {**meta, "name": name + "-gateway"},
        "spec": {
            "podSelector": {"matchLabels": labels},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": gateway_namespace}
                            },
                            "podSelector": {"matchLabels": {key: value}},
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 8443}],
                }
            ],
        },
    }
    # DNS, Temporal and telemetry require separate reviewed control-plane egress.
    # No broad rule is added automatically; start remains withheld.
    return [sa, deny, gateway, deployment]


def catalog(root: Path, manifest: Manifest, evidence: dict) -> dict:
    validate_evidence(root, manifest, evidence)
    return {
        "apiVersion": "zetra.dev/catalog-v1alpha1",
        "agent": manifest.raw,
        "risk": risk(manifest),
        "sourceHash": fingerprint(root),
        "evidenceHash": hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(),
        "status": "candidate-not-published",
        "requiredBeforePublication": [
            "Independent risk approval",
            "Signed image and provenance verification",
            "Signed evidence and policy bundle",
            "Live negative-path qualification",
            "Kill-switch exercise",
        ],
        "createdAt": datetime.now(UTC).isoformat(),
    }


def qualify(context: str, namespace: str, kubeconfig: str | None = None) -> dict:
    """Read-only capability discovery; CRD presence never proves enforcement."""
    dns_label(namespace)
    if not context or context.startswith("-"):
        raise ContractError("Explicit kube context required")
    if kubeconfig and not Path(kubeconfig).is_file():
        raise ContractError("Explicit kubeconfig file not found")
    commands = {
        "server": ["get", "--raw", "/version"],
        "crds": ["get", "customresourcedefinitions", "-o", "json"],
        "pods": ["get", "pods", "-n", namespace, "-o", "json"],
        "networkPolicies": ["get", "networkpolicies", "-n", namespace, "-o", "json"],
    }
    results = {}
    for name, args in commands.items():
        try:
            config_args = ["--kubeconfig", kubeconfig] if kubeconfig else []
            proc = subprocess.run(
                ["kubectl", *config_args, "--context", context, "--request-timeout=10s", *args],
                text=True,
                capture_output=True,
                timeout=15,
            )
            if proc.returncode:
                results[name] = {"status": "failed", "error": proc.stderr.strip()[:2000]}
            else:
                result = json.loads(proc.stdout)
                if name == "crds":
                    result = [p["metadata"]["name"] for p in result.get("items", [])]
                elif name == "pods":
                    result = [
                        {
                            "name": p["metadata"]["name"],
                            "phase": p.get("status", {}).get("phase"),
                            "images": [c["image"] for c in p.get("spec", {}).get("containers", [])],
                        }
                        for p in result.get("items", [])
                    ]
                elif name == "networkPolicies":
                    result = [p["metadata"]["name"] for p in result.get("items", [])]
                results[name] = {"status": "observed", "result": result}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            results[name] = {"status": "failed", "error": str(exc)}
    return {
        "apiVersion": "zetra.dev/qualification-v1alpha1",
        "context": context,
        "namespace": namespace,
        "status": "discovery-only-not-qualified",
        "probes": results,
        "requiredLiveTests": [
            "Forbidden process/file/network denied before first agent instruction",
            "Gateway identity, tool authorization, limits and revocation",
            "OpenShell workload policy enforcement",
            "Tetragon scoped observe/enforce and unsupported-hook handling",
            "Temporal retry/replay and receiver idempotency",
            "OTel end-to-end correlation and redaction",
            "Kill switch revokes pending/in-flight actions",
        ],
        "createdAt": datetime.now(UTC).isoformat(),
    }
