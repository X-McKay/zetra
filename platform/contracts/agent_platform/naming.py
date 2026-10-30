"""Derived names of section 3 (3.3, 3.5, 3.6, 10.2)."""

from __future__ import annotations

import hashlib
import re
import uuid

from agent_platform.constants import DEFAULT_TRUST_DOMAIN, TENANT_PATTERN

TENANT_RE = re.compile(TENANT_PATTERN)
SESSION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def is_tenant(namespace: str) -> bool:
    return bool(TENANT_RE.fullmatch(namespace))


def require_tenant(namespace: str) -> str:
    if not is_tenant(namespace):
        raise ValueError(f"not a tenant namespace: {namespace!r}")
    return namespace


def require_session_id(session_id: str) -> str:
    if not SESSION_RE.fullmatch(session_id) or str(uuid.UUID(session_id)) != session_id:
        raise ValueError("session ID must be a lower-case UUID v4")
    return session_id


def sandbox_name(session_id: str, generation: int) -> str:
    if generation < 1:
        raise ValueError("generation starts at 1")
    return f"s-{hashlib.sha256(session_id.encode()).hexdigest()[:16]}-g{generation}"


def workflow_id(tenant: str, agent: str, session_id: str) -> str:
    return f"session:{tenant}:{agent}:{session_id}"


def task_queue(tenant: str) -> str:
    return f"sessions-{tenant}"


def service_account(agent: str) -> str:
    return f"agent-{agent}"


def client_id(tenant: str, agent: str) -> str:
    return f"agent-{tenant}-{agent}"


def agent_id(tenant: str, agent: str) -> str:
    return f"{tenant}/{agent}"


def spiffe_id(tenant: str, agent: str, trust_domain: str = DEFAULT_TRUST_DOMAIN) -> str:
    return f"spiffe://{trust_domain}/ns/{tenant}/sa/agent-{agent}"


def expected_subject(tenant: str, agent: str) -> str:
    """The projected token `sub`: system:serviceaccount:<namespace>:<sa> (7.3)."""
    return f"system:serviceaccount:{tenant}:agent-{agent}"


def realm_role(backend: str) -> str:
    return f"agent-role-{backend}-read"


def claims_scope(tenant: str, agent: str) -> str:
    return f"agent-claims-{tenant}-{agent}"


def mcp_scope(backend: str) -> str:
    return f"mcp:{backend}"


def delegation_scope(tenant: str, agent: str) -> str:
    return f"delegation:client:{client_id(tenant, agent)}"


def mcp_resource(mcp_resource_base: str, backend: str) -> str:
    return f"{mcp_resource_base}/{backend}/mcp"


def policy_configmap(release: str) -> str:
    return f"policy-{release}"


def podtemplate_configmap(release: str) -> str:
    return f"podtemplate-{release}"
