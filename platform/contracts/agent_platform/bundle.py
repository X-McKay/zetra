"""The policy bundle of section 6.3, compiled from an Agent and an AgentRelease.

The same function runs at build time (`agentctl policy-digest`) on the YAML in git and
in the controller on the API object. The API server applies the CRD defaults; a file in
git does not, so every default is applied here too and both paths yield one digest.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from agent_platform import naming
from agent_platform.constants import (
    INFERENCE_HOST,
    MAX_BOUNDARIES,
    MCP_HOST,
    SANDBOX_TTL_SECONDS,
    TOKEN_HOST,
    TOKEN_ROUTE_PATH,
    TOKENS_PER_DAY,
    TOKENS_PER_SESSION,
)

BUNDLE_VERSION = 1


def canonical_json(doc: Any) -> bytes:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest_hex(doc: Any) -> str:
    return hashlib.sha256(canonical_json(doc)).hexdigest()


def policy_digest(bundle: dict[str, Any]) -> str:
    return "sha256:" + digest_hex(bundle)


def _identity(spec: dict[str, Any]) -> dict[str, Any]:
    identity = spec.get("identity") or {}
    mode = identity.get("mode")
    if mode not in ("agent", "on-behalf-of"):
        raise ValueError("spec.identity.mode must be agent or on-behalf-of")
    obo = identity.get("onBehalfOf") or {}
    fallback = obo.get("fallback", "deny")
    if fallback not in ("deny", "agent"):
        raise ValueError("spec.identity.onBehalfOf.fallback must be deny or agent")
    return {"mode": mode, "required": bool(obo.get("required", True)), "fallback": fallback}


def _budgets(models: list[dict[str, Any]]) -> dict[str, int]:
    # One budget per agent. With several model entries the smallest declared value wins,
    # so adding a model entry can never widen the agent's budget.
    per_day = [(m.get("budget") or {}).get("tokensPerDay", TOKENS_PER_DAY) for m in models]
    per_session = [
        (m.get("budget") or {}).get("tokensPerSession", TOKENS_PER_SESSION) for m in models
    ]
    return {
        "tokens_per_day": min(per_day, default=TOKENS_PER_DAY),
        "tokens_per_session": min(per_session, default=TOKENS_PER_SESSION),
    }


def compile_bundle(
    agent: dict[str, Any],
    release: dict[str, Any],
    *,
    mcp_resource_base: str,
    trust_domain: str,
) -> dict[str, Any]:
    meta, spec = agent["metadata"], agent["spec"]
    name, tenant = meta["name"], naming.require_tenant(meta["namespace"])
    rmeta, rspec = release["metadata"], release["spec"]
    if rspec.get("agent") != name:
        raise ValueError(
            f"release {rmeta['name']} is for agent {rspec.get('agent')!r}, not {name!r}"
        )
    if rmeta.get("namespace", tenant) != tenant:
        raise ValueError("release and agent must share the tenant namespace")

    identity = _identity(spec)
    caps = spec.get("capabilities") or {}
    models_in = caps.get("models") or []
    mcp_in = caps.get("mcp") or []

    models: dict[str, list[str]] = {}
    for m in models_in:
        if m["backend"] in models:
            raise ValueError(f"model backend {m['backend']} declared twice")
        models[m["backend"]] = sorted(set(m["models"]))

    mcp: dict[str, dict[str, Any]] = {}
    for entry in mcp_in:
        backend = entry["backend"]
        if backend in mcp:
            raise ValueError(f"MCP backend {backend} declared twice")
        delegation = entry.get("delegation", "inherit")
        if delegation == "inherit":
            delegation = identity["mode"]
        if delegation not in ("agent", "on-behalf-of"):
            raise ValueError(f"MCP backend {backend}: invalid delegation {delegation!r}")
        if entry.get("effect") != "remote_read":
            raise ValueError(f"MCP backend {backend}: only effect remote_read is admitted")
        mcp[backend] = {
            "effect": entry["effect"],
            "tools": sorted(set(entry["tools"])),
            "delegation": delegation,
            "resource": naming.mcp_resource(mcp_resource_base, backend),
        }

    # Routes as the gateway serves them (8.3): the token route ends at the APS, one
    # inference prefix per model backend, one MCP path per MCP backend.
    routes: dict[str, list[str]] = {TOKEN_HOST: [TOKEN_ROUTE_PATH]}
    if models:
        routes[INFERENCE_HOST] = sorted(f"/models/{b}/v1/" for b in models)
    if mcp:
        routes[MCP_HOST] = sorted(f"/{b}/mcp" for b in mcp)

    runtime = spec.get("runtime") or {}
    return {
        "version": BUNDLE_VERSION,
        "tenant": tenant,
        "agent": name,
        "release": rmeta["name"],
        "image": rspec["image"],
        "client_id": naming.client_id(tenant, name),
        "spiffe_id": naming.spiffe_id(tenant, name, trust_domain),
        "identity": identity,
        "routes": routes,
        "models": models,
        "mcp": mcp,
        "budgets": _budgets(models_in),
        "runtime": {
            "ttl_seconds": int(runtime.get("ttlSeconds", SANDBOX_TTL_SECONDS)),
            "max_boundaries": int(runtime.get("maxBoundaries", MAX_BOUNDARIES)),
        },
    }


def delegation_required(bundle: dict[str, Any]) -> bool:
    identity = bundle["identity"]
    return identity["mode"] == "on-behalf-of" and identity["required"]
