"""T28 logic: the controller's published digest equals the runner's preflight digest.

The preflight below is the 10.5 reference code with the Kubernetes calls replaced by
objects deserialized through the real Kubernetes Python client from the JSON an API
server returns: the 5.3 manifests as an operator would write them, defaulted the way
the API server defaults NetworkPolicy, plus server-populated metadata.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import yaml
from kubernetes import client

from agent_platform.cli import main as agentctl
from agent_platform.netpol import netpol_digest, render_tenant_policies, tenant_policy_specs

FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_POLICIES = {
    "default-deny-all",
    "sandbox-egress-gateway-only",
    "sandbox-ingress-none",
    "tenant-no-other-egress",
}


class _Response:
    def __init__(self, data):
        self.data = json.dumps(data)


def api_server_defaults(policy: dict) -> dict:
    """NetworkPolicy defaulting (pkg/apis/networking/v1/defaults.go) and server metadata."""
    p = copy.deepcopy(policy)
    spec = p["spec"]
    for rule in spec.get("ingress", []) + spec.get("egress", []):
        for port in rule.get("ports", []):
            port.setdefault("protocol", "TCP")
    if not spec.get("policyTypes"):
        types = ["Ingress"]
        if spec.get("egress"):
            types.append("Egress")
        spec["policyTypes"] = types
    for key in ("ingress", "egress"):
        if key in spec and not spec[key]:
            del spec[key]  # omitempty
    p["metadata"].update(
        uid="5d0a1f2e-0000-4000-8000-000000000000",
        resourceVersion="4242",
        generation=1,
        creationTimestamp="2026-10-30T10:00:00Z",
        managedFields=[{"manager": "registry-controller", "operation": "Update"}],
    )
    return p


def as_api_list(policies: list[dict]):
    body = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicyList",
        "metadata": {"resourceVersion": "4243"},
        "items": [api_server_defaults(p) for p in policies],
    }
    return client.ApiClient().deserialize(_Response(body), "V1NetworkPolicyList").items


# --- 10.5 reference preflight, verbatim apart from the injected inputs ---
def canonical_policy(p) -> str:
    spec = client.ApiClient().sanitize_for_serialization(p.spec)
    return json.dumps(spec, sort_keys=True, separators=(",", ":"))


def preflight_netpol_ok(items, annotation: str | None) -> bool:
    pols = {p.metadata.name: canonical_policy(p) for p in items}
    have = hashlib.sha256(json.dumps(pols, sort_keys=True).encode()).hexdigest()
    return set(pols) == EXPECTED_POLICIES and annotation == f"sha256:{have}"


def test_t28_rendered_policies_pass_runner_preflight():
    items = as_api_list(render_tenant_policies("tenant-a"))
    assert preflight_netpol_ok(items, netpol_digest())


def test_t28_spec_5_3_manifests_as_written_give_the_same_digest():
    # The spec's YAML (flow-style ports, no explicit defaults beyond what it shows),
    # applied through an API server, hashes to the controller's digest.
    written = list(yaml.safe_load_all((FIXTURES / "netpol-5.3.yaml").read_text()))
    assert preflight_netpol_ok(as_api_list(written), netpol_digest())
    assert {p["metadata"]["name"]: p["spec"] for p in written} == tenant_policy_specs()


def test_t28_omitted_defaults_are_filled_by_the_server():
    policies = render_tenant_policies("tenant-a")
    for p in policies:
        for rule in p["spec"].get("egress", []):
            for port in rule["ports"]:
                del port["protocol"]
    assert preflight_netpol_ok(as_api_list(policies), netpol_digest())


def test_t28_extra_allow_all_policy_fails_preflight():
    policies = render_tenant_policies("tenant-a") + [
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "allow-all-egress", "namespace": "tenant-a"},
            "spec": {"podSelector": {}, "egress": [{}], "policyTypes": ["Egress"]},
        }
    ]
    assert not preflight_netpol_ok(as_api_list(policies), netpol_digest())


def test_t28_edited_policy_fails_preflight():
    policies = render_tenant_policies("tenant-a")
    policies[1]["spec"]["egress"][0]["ports"].append({"protocol": "TCP", "port": 8080})
    assert not preflight_netpol_ok(as_api_list(policies), netpol_digest())


def test_t28_missing_policy_fails_preflight():
    policies = render_tenant_policies("tenant-a")[:3]
    assert not preflight_netpol_ok(as_api_list(policies), netpol_digest())


def test_digest_is_namespace_independent():
    a = as_api_list(render_tenant_policies("tenant-a"))
    b = as_api_list(render_tenant_policies("tenant-b"))
    assert [canonical_policy(p) for p in a] == [canonical_policy(p) for p in b]


def test_agentctl_render_netpol(capsys):
    assert agentctl(["render-netpol", "--namespace", "tenant-a"]) == 0
    out = capsys.readouterr().out
    assert f"agentplatform.io/netpol-digest: {netpol_digest()}" in out
    docs = [d for d in yaml.safe_load_all(out) if d]
    assert [d["metadata"]["name"] for d in docs] == sorted(EXPECTED_POLICIES)
