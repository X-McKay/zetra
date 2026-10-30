"""Pure reconcile logic of section 6.4: desired objects and status, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from agent_platform import naming
from agent_platform.bundle import canonical_json
from agent_platform.constants import (
    ANNOTATION_NETPOL_DIGEST,
    API_GROUP,
    APS_SERVICE_ACCOUNT,
    ATTESTOR_SERVICE_ACCOUNT,
    GATEWAY_CA_KEY,
    LABEL_AGENT,
    LABEL_RELEASE,
    LABEL_TENANT,
    PLATFORM_NAMESPACE,
    POD_TEMPLATE_KEY,
    POLICY_BUNDLE_KEY,
    RATELIMIT_CONFIGMAP,
    RATELIMIT_DOMAIN,
    RATELIMIT_KEY,
    RECONCILER_SERVICE_ACCOUNT,
    RUNNER_SERVICE_ACCOUNT,
    TOKENS_PER_DAY,
)
from agent_platform.netpol import netpol_digest, render_tenant_policies

MANAGED_BY = {"app.kubernetes.io/managed-by": "agent-registry-controller"}
APS_TOKEN_ROLE = "agent-policy-service-token"


def owner_ref(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": obj["apiVersion"],
        "kind": obj["kind"],
        "name": obj["metadata"]["name"],
        "uid": obj["metadata"]["uid"],
        "controller": True,
        "blockOwnerDeletion": False,
    }


def _meta(name: str, namespace: str, labels: dict[str, str] | None = None, owner=None) -> dict:
    meta: dict[str, Any] = {
        "name": name,
        "namespace": namespace,
        "labels": dict(MANAGED_BY, **(labels or {})),
    }
    if owner is not None and owner["metadata"].get("uid"):
        meta["ownerReferences"] = [owner_ref(owner)]
    return meta


# ---- Agent ---------------------------------------------------------------------------


def service_account(agent: dict[str, Any]) -> dict[str, Any]:
    name, ns = agent["metadata"]["name"], agent["metadata"]["namespace"]
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": _meta(naming.service_account(name), ns, {LABEL_AGENT: name}, agent),
        "automountServiceAccountToken": False,
    }


def namespace_metadata(tenant: str) -> dict[str, Any]:
    """Merge patch for the tenant namespace: tenant label and netpol digest annotation."""
    return {
        "metadata": {
            "labels": {LABEL_TENANT: tenant},
            "annotations": {ANNOTATION_NETPOL_DIGEST: netpol_digest()},
        }
    }


def network_policies(tenant: str) -> list[dict[str, Any]]:
    out = []
    for policy in render_tenant_policies(tenant):
        policy["metadata"]["labels"] = dict(MANAGED_BY)
        out.append(policy)
    return out


def ca_copy(source: dict[str, Any] | None, tenant: str, name: str) -> dict[str, Any] | None:
    if not source or GATEWAY_CA_KEY not in (source.get("data") or {}):
        return None
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _meta(name, tenant),
        "data": {GATEWAY_CA_KEY: source["data"][GATEWAY_CA_KEY]},
    }


def _role(name: str, tenant: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": _meta(name, tenant),
        "rules": rules,
    }


def _binding(name: str, tenant: str, sa: str) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": _meta(name, tenant),
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
        "subjects": [{"kind": "ServiceAccount", "name": sa, "namespace": PLATFORM_NAMESPACE}],
    }


def _rule(group: str, resources: list[str], verbs: list[str], names: list[str] | None = None):
    rule: dict[str, Any] = {"apiGroups": [group], "resources": resources, "verbs": sorted(verbs)}
    if names is not None:
        rule["resourceNames"] = names
    return rule


def tenant_rbac(tenant: str, agent_names: list[str]) -> tuple[list[dict], list[tuple[str, str]]]:
    """Roles and RoleBindings for the platform principals; returns (objects, absent).

    `absent` lists (kind, name) pairs that must not exist: a Role whose `resourceNames`
    would be empty grants every name, so the APS token Role exists only with agents.
    """
    runner_rules = [
        _rule("", ["pods"], ["create", "get", "list", "watch", "delete"]),
        _rule("", ["pods/exec"], ["get", "create"]),
        _rule("", ["configmaps"], ["get"]),
        _rule(API_GROUP, ["agentreleases"], ["get"]),
        _rule("networking.k8s.io", ["networkpolicies"], ["list"]),
        _rule("", ["namespaces"], ["get"], [tenant]),
    ]
    reconciler_rules = [
        _rule("", ["pods"], ["get", "list", "watch", "delete"]),
        *runner_rules[2:],
    ]
    roles = {
        RUNNER_SERVICE_ACCOUNT: (RUNNER_SERVICE_ACCOUNT, runner_rules),
        RECONCILER_SERVICE_ACCOUNT: (RECONCILER_SERVICE_ACCOUNT, reconciler_rules),
        ATTESTOR_SERVICE_ACCOUNT: (ATTESTOR_SERVICE_ACCOUNT, [_rule("", ["pods"], ["patch"])]),
    }
    absent: list[tuple[str, str]] = []
    sas = sorted(naming.service_account(n) for n in set(agent_names))
    if sas:
        roles[APS_TOKEN_ROLE] = (
            APS_SERVICE_ACCOUNT,
            [_rule("", ["serviceaccounts/token"], ["create"], sas)],
        )
    else:
        absent += [("RoleBinding", APS_TOKEN_ROLE), ("Role", APS_TOKEN_ROLE)]
    objects = []
    for role_name, (sa, rules) in roles.items():
        objects += [_role(role_name, tenant, rules), _binding(role_name, tenant, sa)]
    return objects, absent


# ---- Conditions ----------------------------------------------------------------------


def set_condition(
    conditions: list[dict[str, Any]] | None,
    type_: str,
    status: bool | None,
    reason: str,
    message: str,
    now: str,
) -> list[dict[str, Any]]:
    value = "Unknown" if status is None else ("True" if status else "False")
    out = [dict(c) for c in conditions or [] if c.get("type") != type_]
    prev = next((c for c in conditions or [] if c.get("type") == type_), None)
    changed = prev is None or prev.get("status") != value
    out.append(
        {
            "type": type_,
            "status": value,
            "reason": reason,
            "message": message,
            "lastTransitionTime": now if changed else prev.get("lastTransitionTime", now),
        }
    )
    return sorted(out, key=lambda c: c["type"])


def condition_true(obj: dict[str, Any] | None, type_: str) -> bool:
    for c in ((obj or {}).get("status") or {}).get("conditions") or []:
        if c.get("type") == type_:
            return c.get("status") == "True"
    return False


def agent_enabled(agent: dict[str, Any] | None) -> bool:
    return bool(agent) and agent["spec"].get("enabled", True) is True


# ---- AgentRelease --------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseFacts:
    approved: bool
    signature_verified: bool
    policy_drift: bool
    agent_enabled: bool
    agent_ready: bool


def release_ready(f: ReleaseFacts) -> bool:
    """6.4 step 5, exactly."""
    return (
        f.approved
        and f.signature_verified
        and not f.policy_drift
        and f.agent_enabled
        and f.agent_ready
    )


def policy_configmap(release: dict[str, Any], bundle: dict[str, Any]) -> dict[str, Any]:
    name, ns = release["metadata"]["name"], release["metadata"]["namespace"]
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _meta(
            naming.policy_configmap(name),
            ns,
            {LABEL_RELEASE: name, LABEL_AGENT: release["spec"]["agent"]},
            release,
        ),
        "data": {POLICY_BUNDLE_KEY: canonical_json(bundle).decode()},
    }


def podtemplate_configmap(release: dict[str, Any], pod: dict[str, Any]) -> dict[str, Any]:
    name, ns = release["metadata"]["name"], release["metadata"]["namespace"]
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _meta(
            naming.podtemplate_configmap(name),
            ns,
            {LABEL_RELEASE: name, LABEL_AGENT: release["spec"]["agent"]},
            release,
        ),
        "data": {POD_TEMPLATE_KEY: yaml.safe_dump(pod, sort_keys=False)},
    }


# ---- Rate limit (8.9) ----------------------------------------------------------------


def ratelimit_yaml(budgets: dict[str, int]) -> str:
    """Envoy ratelimit config from {agent_id: tokens_per_day} of every ready release."""
    descriptors: list[dict[str, Any]] = [
        {"key": "agent", "rate_limit": {"unit": "day", "requests_per_unit": TOKENS_PER_DAY}}
    ]
    for agent_id in sorted(budgets):
        descriptors.append(
            {
                "key": "agent",
                "value": agent_id,
                "rate_limit": {"unit": "day", "requests_per_unit": budgets[agent_id]},
            }
        )
    return yaml.safe_dump({"domain": RATELIMIT_DOMAIN, "descriptors": descriptors}, sort_keys=False)


def ratelimit_configmap(budgets: dict[str, int]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _meta(RATELIMIT_CONFIGMAP, PLATFORM_NAMESPACE),
        "data": {RATELIMIT_KEY: ratelimit_yaml(budgets)},
    }
