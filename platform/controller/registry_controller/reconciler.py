"""Section 6.4 reconcile rules over the Kubernetes, IdP and signature ports.

Every method is idempotent and safe to re-run on any change or on the resync timer.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import UTC, datetime
from typing import Any, Callable

from agent_platform import naming
from agent_platform.bundle import compile_bundle, delegation_required, digest_hex
from agent_platform.constants import (
    AGENT_SUBJECTS_CONFIGMAP,
    CLIENT_TYPE_AGENT,
    GROUP_VERSION,
    MCP_NAMESPACE,
    POLICY_BUNDLE_KEY,
    TENANT_NETWORK_POLICIES,
)
from agent_platform.podtemplate import render_pod_template
from registry_controller import desired
from registry_controller.config import ControllerConfig
from registry_controller.idp import AgentClientSpec, IdpAdapter
from registry_controller.kube import KubePort
from registry_controller.signature import SignatureVerifier

log = logging.getLogger(__name__)
NETPOL_API = "networking.k8s.io/v1"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _live(obj: dict[str, Any] | None) -> bool:
    return bool(obj) and not obj["metadata"].get("deletionTimestamp")


def _differs(want: dict[str, Any], have: dict[str, Any]) -> bool:
    for key, value in want.items():
        if key in ("apiVersion", "kind", "metadata", "status"):
            continue
        if have.get(key) != value:
            return True
    for part in ("labels", "annotations"):
        cur = have["metadata"].get(part) or {}
        if any(cur.get(k) != v for k, v in (want["metadata"].get(part) or {}).items()):
            return True
    return False


class Reconciler:
    def __init__(
        self,
        kube: KubePort,
        idp: IdpAdapter,
        verifier: SignatureVerifier,
        cfg: ControllerConfig,
        now: Callable[[], str] = _now,
    ) -> None:
        self.kube = kube
        self.idp = idp
        self.verifier = verifier
        self.cfg = cfg
        self.now = now

    # -- helpers -----------------------------------------------------------------------

    def ensure(self, want: dict[str, Any]) -> None:
        meta = want["metadata"]
        have = self.kube.get(want["apiVersion"], want["kind"], meta.get("namespace"), meta["name"])
        if have is None:
            self.kube.create(want)
            return
        if not _differs(want, have):
            return
        # Full replace: fields someone else added to an owned object are reverted too.
        body = copy.deepcopy(want)
        merged = copy.deepcopy(have["metadata"])
        for part in ("labels", "annotations"):
            merged[part] = dict(merged.get(part) or {}, **(meta.get(part) or {}))
        if "ownerReferences" in meta:
            merged["ownerReferences"] = meta["ownerReferences"]
        merged.pop("managedFields", None)
        body["metadata"] = merged
        self.kube.replace(body)

    def agents_in(self, tenant: str) -> list[dict[str, Any]]:
        return [a for a in self.kube.list(GROUP_VERSION, "Agent", tenant) if _live(a)]

    # -- tenant namespace (6.4 Agent step 2, 9.1, 10.6, 10.7, 5.5) --------------------

    def reconcile_tenant(self, tenant: str) -> list[str]:
        """Converge the namespace; return the problems that keep agents from Ready."""
        naming.require_tenant(tenant)
        problems: list[str] = []
        self.kube.merge_patch("v1", "Namespace", None, tenant, desired.namespace_metadata(tenant))

        for policy in desired.network_policies(tenant):
            self.ensure(policy)
        for policy in self.kube.list(NETPOL_API, "NetworkPolicy", tenant):
            name = policy["metadata"]["name"]
            if name not in TENANT_NETWORK_POLICIES:
                log.warning("deleting foreign NetworkPolicy %s/%s", tenant, name)
                self.kube.delete(NETPOL_API, "NetworkPolicy", tenant, name)

        source = self.kube.get(
            "v1", "ConfigMap", self.cfg.ca_source_namespace, self.cfg.platform.ca_configmap
        )
        ca = desired.ca_copy(source, tenant, self.cfg.platform.ca_configmap)
        if ca is None:
            problems.append(
                f"ConfigMap {self.cfg.ca_source_namespace}/{self.cfg.platform.ca_configmap} "
                "with key ca.crt is missing"
            )
        else:
            self.ensure(ca)

        names = [a["metadata"]["name"] for a in self.agents_in(tenant)]
        objects, absent = desired.tenant_rbac(tenant, names)
        for obj in objects:
            self.ensure(obj)
        for kind, name in absent:
            self.kube.delete("rbac.authorization.k8s.io/v1", kind, tenant, name)
        return problems

    # -- Agent -------------------------------------------------------------------------

    def _client_spec(self, tenant: str, name: str, spec: dict[str, Any]) -> AgentClientSpec:
        backends = sorted({m["backend"] for m in (spec.get("capabilities") or {}).get("mcp") or []})
        return AgentClientSpec(
            client_id=naming.client_id(tenant, name),
            expected_subject=naming.expected_subject(tenant, name),
            claims_scope=naming.claims_scope(tenant, name),
            claims={
                "client_type": CLIENT_TYPE_AGENT,
                "agent_id": naming.agent_id(tenant, name),
                "tenant": tenant,
                "spiffe_id": naming.spiffe_id(tenant, name, self.cfg.platform.trust_domain),
            },
            mcp_scopes={
                naming.mcp_scope(b): naming.mcp_resource(self.cfg.platform.mcp_resource_base, b)
                for b in backends
            },
            realm_roles=frozenset(naming.realm_role(b) for b in backends),
        )

    def _set_subject(self, subject: str, value: str | None) -> None:
        have = self.kube.get("v1", "ConfigMap", MCP_NAMESPACE, AGENT_SUBJECTS_CONFIGMAP)
        if have is None:
            if value is not None:
                self.kube.create(
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {
                            "name": AGENT_SUBJECTS_CONFIGMAP,
                            "namespace": MCP_NAMESPACE,
                            "labels": dict(desired.MANAGED_BY),
                        },
                        "data": {subject: value},
                    }
                )
            return
        if (have.get("data") or {}).get(subject) != value:
            self.kube.merge_patch(
                "v1",
                "ConfigMap",
                MCP_NAMESPACE,
                AGENT_SUBJECTS_CONFIGMAP,
                {"data": {subject: value}},
            )

    def reconcile_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        meta, spec = agent["metadata"], agent["spec"]
        name, tenant = meta["name"], meta["namespace"]
        old = agent.get("status") or {}
        now = self.now()
        conditions = old.get("conditions") or []
        enabled = desired.agent_enabled(agent)
        conditions = desired.set_condition(
            conditions,
            "Enabled",
            enabled,
            "Enabled" if enabled else "Disabled",
            "" if enabled else "spec.enabled is false; every release is denied",
            now,
        )
        status: dict[str, Any] = {}

        if not naming.is_tenant(tenant):
            conditions = desired.set_condition(
                conditions,
                "Ready",
                False,
                "InvalidTenant",
                f"namespace {tenant} does not match the tenant pattern",
                now,
            )
            status["conditions"] = conditions
            self.kube.patch_status(GROUP_VERSION, "Agent", tenant, name, status)
            return status

        self.ensure(desired.service_account(agent))
        problems = self.reconcile_tenant(tenant)

        reason, message = "Reconciled", ""
        try:
            subject = self.idp.ensure_agent(self._client_spec(tenant, name, spec))
        except Exception as exc:  # any IdP failure leaves the agent not Ready
            log.warning("IdP reconcile failed for %s/%s: %s", tenant, name, type(exc).__name__)
            subject = None
            reason, message = "IdpError", f"identity provider: {type(exc).__name__}"
        if subject:
            previous = old.get("idpSubject")
            if previous and previous != subject:
                self._set_subject(previous, None)
            self._set_subject(subject, naming.agent_id(tenant, name))
            status["idpSubject"] = subject
        if problems and subject:
            reason, message = "TenantIncomplete", "; ".join(problems)

        status.update(
            serviceAccount=naming.service_account(name),
            clientId=naming.client_id(tenant, name),
            spiffeId=naming.spiffe_id(tenant, name, self.cfg.platform.trust_domain),
        )
        ready = bool(subject) and not problems
        status["conditions"] = desired.set_condition(
            conditions, "Ready", ready, "Ready" if ready else reason, message, now
        )
        self.kube.patch_status(GROUP_VERSION, "Agent", tenant, name, status)

        self.reconcile_releases_of(tenant, name)
        return status

    def delete_agent(self, agent: dict[str, Any]) -> None:
        meta = agent["metadata"]
        name, tenant = meta["name"], meta["namespace"]
        subject = (agent.get("status") or {}).get("idpSubject")
        if subject:
            self._set_subject(subject, None)
        if naming.is_tenant(tenant):
            self.idp.delete_agent(naming.client_id(tenant, name), naming.claims_scope(tenant, name))
            self.kube.delete("v1", "ServiceAccount", tenant, naming.service_account(name))
            self.reconcile_tenant(tenant)
            self.reconcile_releases_of(tenant, name)

    # -- AgentRelease ------------------------------------------------------------------

    def reconcile_releases_of(self, tenant: str, agent_name: str) -> None:
        for release in self.kube.list(GROUP_VERSION, "AgentRelease", tenant):
            if _live(release) and release["spec"].get("agent") == agent_name:
                self.reconcile_release(release, regenerate=False)
        self.regenerate_ratelimit()

    def reconcile_release(self, release: dict[str, Any], regenerate: bool = True) -> dict:
        meta, spec = release["metadata"], release["spec"]
        name, tenant = meta["name"], meta["namespace"]
        old = release.get("status") or {}
        now = self.now()
        conds = old.get("conditions") or []
        status: dict[str, Any] = {}

        try:
            signed = bool(
                self.verifier.verify(spec["image"], spec["manifestSha256"], spec["policySha256"])
            )
        except Exception as exc:  # fail closed
            log.warning(
                "signature verification error for %s/%s: %s", tenant, name, type(exc).__name__
            )
            signed = False
        status["signatureVerified"] = signed
        conds = desired.set_condition(
            conds,
            "SignatureVerified",
            signed,
            "Verified" if signed else "VerificationFailed",
            "",
            now,
        )

        agent = self.kube.get(GROUP_VERSION, "Agent", tenant, spec["agent"])
        if not _live(agent) or not naming.is_tenant(tenant):
            status["ready"] = False
            status["conditions"] = desired.set_condition(
                conds, "Ready", False, "AgentMissing", f"Agent {spec['agent']} not found", now
            )
            self.kube.patch_status(GROUP_VERSION, "AgentRelease", tenant, name, status)
            return self._finish(status, regenerate)

        pcfg = self.cfg.platform
        try:
            bundle = compile_bundle(
                agent,
                release,
                mcp_resource_base=pcfg.mcp_resource_base,
                trust_domain=pcfg.trust_domain,
            )
            drift = digest_hex(bundle) != spec["policySha256"]
            drift_reason = "DigestMismatch" if drift else "NoDrift"
        except (ValueError, KeyError) as exc:
            bundle, drift, drift_reason = None, True, f"CompileError: {exc}"

        status["policyDrift"] = drift
        conds = desired.set_condition(
            conds,
            "PolicyDrift",
            drift,
            drift_reason.split(":")[0],
            drift_reason if drift else "",
            now,
        )
        if not drift:
            digest = "sha256:" + spec["policySha256"]
            policy_cm = desired.policy_configmap(release, bundle)
            self.ensure(policy_cm)
            pod = render_pod_template(agent, release, bundle, digest, pcfg)
            template_cm = desired.podtemplate_configmap(release, pod)
            self.ensure(template_cm)
            status.update(
                policyDigest=digest,
                policyBundleConfigMap=policy_cm["metadata"]["name"],
                identityMode=bundle["identity"]["mode"],
                delegationRequired=delegation_required(bundle),
                podTemplateConfigMap=template_cm["metadata"]["name"],
            )
        # On drift policyDigest and the ConfigMaps are left exactly as they were.

        facts = desired.ReleaseFacts(
            approved=(spec.get("approval") or {}).get("approved") is True,
            signature_verified=signed,
            policy_drift=drift,
            agent_enabled=desired.agent_enabled(agent),
            agent_ready=desired.condition_true(agent, "Ready"),
        )
        ready = desired.release_ready(facts)
        status["ready"] = ready
        status["conditions"] = desired.set_condition(
            conds, "Ready", ready, "Ready" if ready else _not_ready_reason(facts), "", now
        )
        self.kube.patch_status(GROUP_VERSION, "AgentRelease", tenant, name, status)
        return self._finish(status, regenerate)

    def _finish(self, status: dict, regenerate: bool) -> dict:
        if regenerate:
            self.regenerate_ratelimit()
        return status

    # -- rate limit (8.9) --------------------------------------------------------------

    def regenerate_ratelimit(self) -> None:
        budgets: dict[str, int] = {}
        for release in self.kube.list(GROUP_VERSION, "AgentRelease"):
            if not _live(release) or (release.get("status") or {}).get("ready") is not True:
                continue
            tenant, name = release["metadata"]["namespace"], release["metadata"]["name"]
            cm = self.kube.get("v1", "ConfigMap", tenant, naming.policy_configmap(name))
            try:
                bundle = json.loads(cm["data"][POLICY_BUNDLE_KEY])
                budgets[naming.agent_id(tenant, bundle["agent"])] = int(
                    bundle["budgets"]["tokens_per_day"]
                )
            except (TypeError, KeyError, ValueError):
                log.warning("ready release %s/%s has no readable bundle", tenant, name)
        self.ensure(desired.ratelimit_configmap(budgets))


def _not_ready_reason(f: desired.ReleaseFacts) -> str:
    if not f.approved:
        return "NotApproved"
    if not f.signature_verified:
        return "SignatureNotVerified"
    if f.policy_drift:
        return "PolicyDrift"
    if not f.agent_enabled:
        return "AgentDisabled"
    return "AgentNotReady"
