from __future__ import annotations

import copy
import dataclasses
import itertools
import json

import pytest
import yaml

from agent_platform.bundle import compile_bundle, digest_hex
from agent_platform.netpol import netpol_digest, tenant_policy_specs
from registry_controller import desired
from registry_controller.signature import Verification
from tests.conftest import AGENT, World, make_release
from tests.fakes import FakeVerifier

GV = "agentplatform.io/v1alpha1"


def setup(world, agent=AGENT, **release_spec):
    world.add(agent)
    world.add(make_release(agent, world.cfg, **release_spec))
    world.reconcile_agent()
    return world.release()


# ---- Agent rules 1-7 -----------------------------------------------------------------


def test_agent_service_account_and_status(world):
    setup(world)
    sa = world.get("v1", "ServiceAccount", "tenant-a", "agent-investigator")
    assert sa["automountServiceAccountToken"] is False
    assert sa["metadata"]["labels"]["agentplatform.io/agent"] == "investigator"
    status = world.agent()["status"]
    assert status["serviceAccount"] == "agent-investigator"
    assert status["clientId"] == "agent-tenant-a-investigator"
    assert status["spiffeId"] == "spiffe://cluster.local/ns/tenant-a/sa/agent-investigator"
    assert status["idpSubject"]
    conds = {c["type"]: c for c in status["conditions"]}
    assert conds["Ready"]["status"] == "True" and conds["Enabled"]["status"] == "True"


def test_idp_client_spec(world):
    setup(world)
    spec = world.idp.clients["agent-tenant-a-investigator"]
    assert spec.expected_subject == "system:serviceaccount:tenant-a:agent-investigator"
    assert spec.claims == {
        "client_type": "agent",
        "agent_id": "tenant-a/investigator",
        "tenant": "tenant-a",
        "spiffe_id": "spiffe://cluster.local/ns/tenant-a/sa/agent-investigator",
    }
    assert spec.claims_scope == "agent-claims-tenant-a-investigator"
    assert spec.mcp_scopes == {"mcp:kubernetes": "https://mcp.example.com/kubernetes/mcp"}
    assert spec.realm_roles == {"agent-role-kubernetes-read"}


def test_tenant_label_netpols_and_digest_annotation(world):
    setup(world)
    ns = world.get("v1", "Namespace", None, "tenant-a")
    assert ns["metadata"]["labels"]["agentplatform.io/tenant"] == "tenant-a"
    assert ns["metadata"]["annotations"]["agentplatform.io/netpol-digest"] == netpol_digest()
    pols = world.kube.list("networking.k8s.io/v1", "NetworkPolicy", "tenant-a")
    assert {p["metadata"]["name"]: p["spec"] for p in pols} == tenant_policy_specs()


def test_t28_netpol_drift_is_reverted_and_extras_deleted(world):
    setup(world)
    edited = world.kube.obj(
        "networking.k8s.io/v1", "NetworkPolicy", "tenant-a", "sandbox-egress-gateway-only"
    )
    edited["spec"]["egress"].append({})  # allow-all egress rule
    world.kube.delete("networking.k8s.io/v1", "NetworkPolicy", "tenant-a", "sandbox-ingress-none")
    world.add(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": "allow-all-egress", "namespace": "tenant-a"},
            "spec": {"podSelector": {}, "egress": [{}], "policyTypes": ["Egress"]},
        }
    )
    ns = world.kube.obj("v1", "Namespace", None, "tenant-a")
    ns["metadata"]["annotations"]["agentplatform.io/netpol-digest"] = "sha256:" + "0" * 64

    assert world.rec.reconcile_tenant("tenant-a") == []
    pols = world.kube.list("networking.k8s.io/v1", "NetworkPolicy", "tenant-a")
    assert {p["metadata"]["name"]: p["spec"] for p in pols} == tenant_policy_specs()
    ns = world.get("v1", "Namespace", None, "tenant-a")
    assert ns["metadata"]["annotations"]["agentplatform.io/netpol-digest"] == netpol_digest()


def test_reconcile_is_idempotent(world):
    setup(world)
    world.kube.writes.clear()
    world.reconcile_agent()
    assert [w for w in world.kube.writes if w[0] != "patch"] == []


def test_gateway_ca_copied(world):
    setup(world)
    cm = world.get("v1", "ConfigMap", "tenant-a", "agent-gateway-ca")
    src = world.get("v1", "ConfigMap", "agent-platform", "agent-gateway-ca")
    assert cm["data"] == {"ca.crt": src["data"]["ca.crt"]}


def test_missing_gateway_ca_keeps_agent_not_ready(world):
    world.kube.delete("v1", "ConfigMap", "agent-platform", "agent-gateway-ca")
    release = setup(world)
    cond = {c["type"]: c for c in world.agent()["status"]["conditions"]}["Ready"]
    assert cond["status"] == "False" and cond["reason"] == "TenantIncomplete"
    assert release["status"]["ready"] is False


def test_per_tenant_rbac(world):
    setup(world)
    other = copy.deepcopy(AGENT)
    other["metadata"]["name"] = "triage"
    world.add(other)
    world.reconcile_agent("triage")
    role = world.get(
        "rbac.authorization.k8s.io/v1", "Role", "tenant-a", "agent-policy-service-token"
    )
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["serviceaccounts/token"],
            "verbs": ["create"],
            "resourceNames": ["agent-investigator", "agent-triage"],
        }
    ]
    binding = world.get(
        "rbac.authorization.k8s.io/v1", "RoleBinding", "tenant-a", "agent-policy-service-token"
    )
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "agent-policy-service", "namespace": "agent-platform"}
    ]
    runner = world.get("rbac.authorization.k8s.io/v1", "Role", "tenant-a", "session-runner")
    rules = {(r["apiGroups"][0], r["resources"][0]): r for r in runner["rules"]}
    assert rules[("", "pods")]["verbs"] == ["create", "delete", "get", "list", "watch"]
    assert rules[("", "pods/exec")]["verbs"] == ["create", "get"]
    assert rules[("", "namespaces")]["resourceNames"] == ["tenant-a"]
    assert rules[("networking.k8s.io", "networkpolicies")]["verbs"] == ["list"]
    assert rules[("agentplatform.io", "agentreleases")]["verbs"] == ["get"]
    assert not any("ephemeralcontainers" in r["resources"][0] for r in runner["rules"])
    attestor = world.get("rbac.authorization.k8s.io/v1", "Role", "tenant-a", "netpol-attestor")
    assert attestor["rules"] == [{"apiGroups": [""], "resources": ["pods"], "verbs": ["patch"]}]
    reconciler = world.get("rbac.authorization.k8s.io/v1", "Role", "tenant-a", "cleanup-reconciler")
    assert all(r["resources"] != ["pods/exec"] for r in reconciler["rules"])
    assert "create" not in reconciler["rules"][0]["verbs"]


def test_aps_token_role_never_has_empty_resource_names(world):
    setup(world)
    agent = world.agent()
    world.kube.delete(GV, "Agent", "tenant-a", "investigator")
    world.rec.delete_agent(agent)
    assert (
        world.get("rbac.authorization.k8s.io/v1", "Role", "tenant-a", "agent-policy-service-token")
        is None
    )
    assert (
        world.get("rbac.authorization.k8s.io/v1", "Role", "tenant-a", "session-runner") is not None
    )


def test_agent_subjects_configmap(world):
    setup(world)
    subject = world.agent()["status"]["idpSubject"]
    cm = world.get("v1", "ConfigMap", "mcp", "agent-subjects")
    assert cm["data"] == {subject: "tenant-a/investigator"}
    agent = world.agent()
    world.kube.delete(GV, "Agent", "tenant-a", "investigator")
    world.rec.delete_agent(agent)
    assert subject not in (world.get("v1", "ConfigMap", "mcp", "agent-subjects").get("data") or {})
    assert "agent-tenant-a-investigator" not in world.idp.clients
    assert world.release()["status"]["ready"] is False


def test_idp_failure_keeps_agent_and_releases_not_ready(world):
    world.idp.fail = True
    release = setup(world)
    cond = {c["type"]: c for c in world.agent()["status"]["conditions"]}["Ready"]
    assert cond["status"] == "False" and cond["reason"] == "IdpError"
    assert release["status"]["ready"] is False


def test_non_tenant_namespace_is_refused(world):
    agent = copy.deepcopy(AGENT)
    agent["metadata"]["namespace"] = "default"
    world.add({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "default"}})
    world.add(agent)
    world.rec.reconcile_agent(world.agent(ns="default"))
    cond = {c["type"]: c for c in world.agent(ns="default")["status"]["conditions"]}["Ready"]
    assert cond["reason"] == "InvalidTenant"
    assert world.idp.clients == {}


# ---- AgentRelease rules 1-6 ----------------------------------------------------------


def test_ready_release_status_and_configmaps(world):
    release = setup(world)
    status = release["status"]
    spec = release["spec"]
    assert status["ready"] is True
    assert status["signatureVerified"] is True and status["policyDrift"] is False
    assert status["policyDigest"] == "sha256:" + spec["policySha256"]
    assert status["identityMode"] == "on-behalf-of" and status["delegationRequired"] is True
    assert status["policyBundleConfigMap"] == "policy-investigator-r1"
    assert status["podTemplateConfigMap"] == "podtemplate-investigator-r1"
    cm = world.get("v1", "ConfigMap", "tenant-a", "policy-investigator-r1")
    raw = cm["data"]["bundle.json"]
    assert "sha256:" + digest_hex(json.loads(raw)) == status["policyDigest"]
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    pod = yaml.safe_load(
        world.get("v1", "ConfigMap", "tenant-a", "podtemplate-investigator-r1")["data"]["pod.yaml"]
    )
    assert (
        pod["metadata"]["annotations"]["agentplatform.io/policy-digest"] == status["policyDigest"]
    )
    assert pod["spec"]["containers"][0]["image"] == spec["image"]
    assert pod["spec"]["initContainers"][0]["image"] == world.cfg.platform.egress_guard_image
    assert pod["metadata"]["labels"]["agentplatform.io/release"] == "investigator-r1"


def test_t20_agent_edit_after_build_drifts_release(world):
    before = setup(world)["status"]
    cm_before = world.get("v1", "ConfigMap", "tenant-a", "policy-investigator-r1")["data"]
    agent = world.kube.obj(GV, "Agent", "tenant-a", "investigator")
    agent["spec"]["capabilities"]["mcp"][0]["tools"].append("secrets_get")
    world.reconcile_agent()
    after = world.release()["status"]
    assert after["policyDrift"] is True
    assert after["ready"] is False
    assert after["policyDigest"] == before["policyDigest"]
    conds = {c["type"]: c for c in after["conditions"]}
    assert conds["PolicyDrift"]["status"] == "True"
    assert conds["Ready"]["reason"] == "PolicyDrift"
    assert world.get("v1", "ConfigMap", "tenant-a", "policy-investigator-r1")["data"] == cm_before


def test_t20_new_release_with_new_digest_is_ready_again(world):
    setup(world)
    agent = world.kube.obj(GV, "Agent", "tenant-a", "investigator")
    agent["spec"]["capabilities"]["mcp"][0]["tools"].append("secrets_get")
    world.reconcile_agent()
    world.add(make_release(world.agent(), world.cfg, name="investigator-r2"))
    status = world.rec.reconcile_release(world.release("investigator-r2"))
    assert status["ready"] is True
    assert world.release()["status"]["ready"] is False


def test_tool_reordering_does_not_drift(world):
    setup(world)
    agent = world.kube.obj(GV, "Agent", "tenant-a", "investigator")
    agent["spec"]["capabilities"]["mcp"][0]["tools"].reverse()
    world.reconcile_agent()
    assert world.release()["status"]["ready"] is True


def test_disabled_agent(world):
    agent = copy.deepcopy(AGENT)
    release = setup(world, agent)
    digest = release["status"]["policyDigest"]
    world.kube.obj(GV, "Agent", "tenant-a", "investigator")["spec"]["enabled"] = False
    world.reconcile_agent()
    status = world.release()["status"]
    assert status["ready"] is False and status["policyDrift"] is False
    assert status["policyDigest"] == digest
    assert {c["type"]: c for c in status["conditions"]}["Ready"]["reason"] == "AgentDisabled"
    enabled = {c["type"]: c for c in world.agent()["status"]["conditions"]}["Enabled"]
    assert enabled["status"] == "False"


def test_unapproved_release(world):
    release = setup(world, approval={"approved": False})
    assert release["status"]["ready"] is False
    assert {c["type"]: c for c in release["status"]["conditions"]}["Ready"][
        "reason"
    ] == "NotApproved"


def test_signature_failure_fails_closed(world):
    world.verifier.ok = False
    release = setup(world)
    assert release["status"]["signatureVerified"] is False and release["status"]["ready"] is False

    class Broken:
        def verify(self, *a):
            raise TimeoutError

    world.rec.verifier = Broken()
    status = world.rec.reconcile_release(world.release())
    assert status["signatureVerified"] is False and status["ready"] is False


def test_release_for_missing_agent(world):
    world.add(make_release(AGENT, world.cfg))
    status = world.rec.reconcile_release(world.release())
    assert status["ready"] is False
    assert {c["type"]: c for c in status["conditions"]}["Ready"]["reason"] == "AgentMissing"


@pytest.mark.parametrize("facts", list(itertools.product([False, True], repeat=5)))
def test_ready_formula_truth_table(facts):
    f = desired.ReleaseFacts(*facts)
    approved, signed, drift, enabled, agent_ready = facts
    assert desired.release_ready(f) is (
        approved and signed and not drift and enabled and agent_ready
    )


# ---- rate limit (8.9) ----------------------------------------------------------------


def test_ratelimit_config_from_ready_releases(world):
    setup(world)
    other = copy.deepcopy(AGENT)
    other["metadata"] = {"name": "triage", "namespace": "tenant-b"}
    other["spec"]["capabilities"]["models"][0]["budget"] = {"tokensPerDay": 1000}
    world.add(other)
    world.add(make_release(other, world.cfg, name="triage-r1"))
    world.reconcile_agent("triage", "tenant-b")
    doc = yaml.safe_load(
        world.get("v1", "ConfigMap", "agent-platform", "ratelimit-config")["data"]["agents.yaml"]
    )
    assert doc == {
        "domain": "agents",
        "descriptors": [
            {"key": "agent", "rate_limit": {"unit": "day", "requests_per_unit": 2000000}},
            {
                "key": "agent",
                "value": "tenant-a/investigator",
                "rate_limit": {"unit": "day", "requests_per_unit": 5000000},
            },
            {
                "key": "agent",
                "value": "tenant-b/triage",
                "rate_limit": {"unit": "day", "requests_per_unit": 1000},
            },
        ],
    }
    world.kube.obj(GV, "Agent", "tenant-b", "triage")["spec"]["enabled"] = False
    world.reconcile_agent("triage", "tenant-b")
    doc = yaml.safe_load(
        world.get("v1", "ConfigMap", "agent-platform", "ratelimit-config")["data"]["agents.yaml"]
    )
    assert [d.get("value") for d in doc["descriptors"]] == [None, "tenant-a/investigator"]


def test_conditions_keep_transition_time():
    c1 = desired.set_condition([], "Ready", True, "Ready", "", "t1")
    c2 = desired.set_condition(c1, "Ready", True, "Ready", "", "t2")
    c3 = desired.set_condition(c2, "Ready", False, "X", "", "t3")
    assert c2[0]["lastTransitionTime"] == "t1" and c3[0]["lastTransitionTime"] == "t3"


def test_bundle_in_configmap_matches_contracts(world, cfg):
    release = setup(world)
    want = compile_bundle(
        world.agent(),
        release,
        mcp_resource_base=cfg.platform.mcp_resource_base,
        trust_domain=cfg.platform.trust_domain,
    )
    have = json.loads(
        world.get("v1", "ConfigMap", "tenant-a", "policy-investigator-r1")["data"]["bundle.json"]
    )
    assert have == want


# ---- keyless signer identity (private Sigstore) ---------------------------------------


def keyless_world(cfg, annotations=None):
    w = World(dataclasses.replace(cfg, signature_profile="keyless"))
    if annotations is not None:
        ns = w.kube.obj("v1", "Namespace", None, "tenant-a")
        ns["metadata"]["annotations"] = annotations
    return w


SIGNER_ANNOTATIONS = {
    "agentplatform.io/signer-issuer": "https://token.actions.githubusercontent.com",
    "agentplatform.io/signer-subject-regexp": "^https://github\\.com/X-McKay/zetra-agents/.+$",
}


def test_keyless_without_namespace_declaration_fails_closed(cfg):
    w = keyless_world(cfg)
    release = setup(w)
    assert release["status"]["signatureVerified"] is False and release["status"]["ready"] is False
    cond = {c["type"]: c for c in release["status"]["conditions"]}["SignatureVerified"]
    assert cond["reason"] == "SignerIdentityMissing"
    assert w.verifier.calls == []  # never asked cosign without an identity to pin


def test_keyless_unanchored_declaration_is_refused(cfg):
    bad = dict(SIGNER_ANNOTATIONS, **{"agentplatform.io/signer-subject-regexp": "zetra-agents"})
    release = setup(keyless_world(cfg, bad))
    assert release["status"]["signatureVerified"] is False


def test_keyless_passes_namespace_signer_and_records_identity(cfg):
    w = keyless_world(cfg, SIGNER_ANNOTATIONS)
    subject = "https://github.com/X-McKay/zetra-agents/.github/workflows/release.yml@refs/tags/investigator/v1"

    class Keyless(FakeVerifier):
        def verify(self, image, m, p, signer=None):
            super().verify(image, m, p, signer)
            return Verification(
                True, "Verified", signer.issuer, subject, signer.required_attestations
            )

    w.rec.verifier = verifier = Keyless()
    release = setup(w)
    signer = verifier.signers[-1]
    assert signer.subject_regexp == SIGNER_ANNOTATIONS["agentplatform.io/signer-subject-regexp"]
    status = release["status"]
    assert status["signatureVerified"] is True and status["ready"] is True
    assert status["verifiedSigner"]["subject"] == subject
    assert len(status["verifiedSigner"]["attestations"]) == 4
