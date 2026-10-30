"""The four tenant NetworkPolicies of section 5.3 and their canonical digest.

The digest must equal what the Session Runner's preflight (10.5) computes from the
objects the API server returns:

    pols = {name: json.dumps(sanitize(spec), sort_keys=True, separators=(",", ":"))}
    sha256(json.dumps(pols, sort_keys=True))

so every spec here is written already in the form the API server returns it after
defaulting: `policyTypes` always explicit, every port with its `protocol`, and no empty
lists or nulls (the client's sanitizer drops `None`, the server omits empty slices).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agent_platform import naming
from agent_platform.constants import (
    GATEWAY_NAMESPACE,
    GATEWAY_POD_LABEL,
    LABEL_SANDBOX,
)


def tenant_policy_specs() -> dict[str, dict[str, Any]]:
    return {
        "default-deny-all": {
            "podSelector": {},
            "policyTypes": ["Ingress", "Egress"],
        },
        "sandbox-egress-gateway-only": {
            "podSelector": {"matchLabels": {LABEL_SANDBOX: "true"}},
            "policyTypes": ["Egress"],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {"kubernetes.io/metadata.name": GATEWAY_NAMESPACE}
                            },
                            "podSelector": {"matchLabels": dict(GATEWAY_POD_LABEL)},
                        }
                    ],
                    "ports": [
                        {"protocol": "TCP", "port": 80},
                        {"protocol": "TCP", "port": 443},
                    ],
                }
            ],
        },
        "sandbox-ingress-none": {
            "podSelector": {"matchLabels": {LABEL_SANDBOX: "true"}},
            "policyTypes": ["Ingress"],
        },
        "tenant-no-other-egress": {
            "podSelector": {
                "matchExpressions": [{"key": LABEL_SANDBOX, "operator": "DoesNotExist"}]
            },
            "policyTypes": ["Egress"],
        },
    }


def render_tenant_policies(namespace: str) -> list[dict[str, Any]]:
    naming.require_tenant(namespace)
    return [
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": namespace},
            "spec": spec,
        }
        for name, spec in tenant_policy_specs().items()
    ]


def canonical_spec(spec: dict[str, Any]) -> str:
    return json.dumps(spec, sort_keys=True, separators=(",", ":"))


def netpol_digest_of(specs: dict[str, dict[str, Any]]) -> str:
    """`sha256:<hex>` over a {policy name: spec} map, exactly as preflight hashes it."""
    pols = {name: canonical_spec(spec) for name, spec in specs.items()}
    return "sha256:" + hashlib.sha256(json.dumps(pols, sort_keys=True).encode()).hexdigest()


def netpol_digest() -> str:
    return netpol_digest_of(tenant_policy_specs())
