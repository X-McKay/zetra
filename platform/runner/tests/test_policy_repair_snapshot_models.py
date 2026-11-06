"""T28 adapter boundary uses real Kubernetes models and the canonical renderer."""

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from kubernetes import client

from runner.conformance_policy_repair_runtime import PolicyRepairOps


@pytest.mark.parametrize("mutation", [None, "extra", "edited", "missing"])
def test_snapshot_hashes_actual_api_models_against_controller_renderer(monkeypatch, mutation):
    # Test-only source access: no new runtime dependency or Kubernetes API calls.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "contracts"))
    from agent_platform.netpol import netpol_digest, render_tenant_policies

    api_client = client.ApiClient()
    documents = render_tenant_policies("tenant-test")
    for index, document in enumerate(documents):
        document["metadata"]["uid"] = "canonical-uid-" + str(index)
    expected = netpol_digest()
    if mutation == "extra":
        documents.append(
            {
                "apiVersion": "networking.k8s.io/v1",
                "kind": "NetworkPolicy",
                "metadata": {"name": "t28-extra", "namespace": "tenant-test", "uid": "extra-uid"},
                "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]},
            }
        )
    elif mutation == "edited":
        next(p for p in documents if p["metadata"]["name"] == "sandbox-egress-gateway-only")[
            "spec"
        ]["egress"][0]["ports"].append({"protocol": "TCP", "port": 8080})
    elif mutation == "missing":
        documents.pop()
    models = api_client.deserialize(
        NS(data=json.dumps({"items": documents})), "V1NetworkPolicyList"
    )
    assert all(isinstance(p, client.V1NetworkPolicy) for p in models.items)
    namespace = client.V1Namespace(
        metadata=client.V1ObjectMeta(annotations={"agentplatform.io/netpol-digest": expected})
    )
    ops = PolicyRepairOps(
        NS(api_client=api_client, read_namespace=lambda *a, **kw: namespace),
        NS(list_namespaced_network_policy=lambda *a, **kw: models),
        None,
        expected,
    )
    snapshot = ops.snapshot()
    assert snapshot["valid"] is (mutation is None)
    assert snapshot["published_digest"] == expected
    assert (snapshot["digest"] == expected) is (mutation is None)
    if mutation == "extra":
        assert snapshot["uids"]["t28-extra"] == "extra-uid"
    assert snapshot["names"] == sorted(document["metadata"]["name"] for document in documents)
