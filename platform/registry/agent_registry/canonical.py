"""Canonical forms and digests. Every digest the registry compares is made here."""

from __future__ import annotations

import hashlib
import json
from typing import Any

EMPTY_AUTHORITY: dict[str, Any] = {}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    """sha256 hex of the canonical form."""
    return hashlib.sha256(canonical(value)).hexdigest()


def authority_of(agent_document: dict[str, Any]) -> dict[str, Any]:
    """The authority an Agent document requests, in one canonical shape.

    This is what an approval is bound to in milestone 1: identity mode, model backends with
    their models and budgets, tool backends with effect, tools and delegation, and runtime
    limits. Owner and description are not authority. Lists are sorted, so order in the
    document does not change the digest.
    """
    spec = agent_document.get("spec") or {}
    identity = spec.get("identity") or {}
    capabilities = spec.get("capabilities") or {}
    runtime = spec.get("runtime") or {}
    models = [
        {
            "backend": m.get("backend"),
            "models": sorted(m.get("models") or []),
            "budget": {
                "tokensPerDay": (m.get("budget") or {}).get("tokensPerDay"),
                "tokensPerSession": (m.get("budget") or {}).get("tokensPerSession"),
            },
        }
        for m in capabilities.get("models") or []
    ]
    mcp = [
        {
            "backend": t.get("backend"),
            "effect": t.get("effect"),
            "tools": sorted(t.get("tools") or []),
            "delegation": t.get("delegation"),
        }
        for t in capabilities.get("mcp") or []
    ]
    return {
        "identity": {"mode": identity.get("mode"), "onBehalfOf": identity.get("onBehalfOf")},
        "models": sorted(models, key=lambda m: str(m["backend"])),
        "mcp": sorted(mcp, key=lambda t: str(t["backend"])),
        "runtime": {
            "runtimeClass": runtime.get("runtimeClass"),
            "cpu": runtime.get("cpu"),
            "memory": runtime.get("memory"),
            "ttlSeconds": runtime.get("ttlSeconds"),
            "maxBoundaries": runtime.get("maxBoundaries"),
        },
    }


def authority_digest(authority: dict[str, Any]) -> str:
    return digest(authority)


def release_document(agent_release: dict[str, Any]) -> dict[str, Any]:
    """An AgentRelease as the registry keeps it: apiVersion, kind, name, namespace and spec,
    with `spec.approval` removed. Approval is the registry's record, not part of the release."""
    meta = agent_release.get("metadata") or {}
    spec = {k: v for k, v in (agent_release.get("spec") or {}).items() if k != "approval"}
    return {
        "apiVersion": agent_release.get("apiVersion"),
        "kind": agent_release.get("kind"),
        "metadata": {"name": meta.get("name"), "namespace": meta.get("namespace")},
        "spec": spec,
    }


def agent_document(agent: dict[str, Any]) -> dict[str, Any]:
    """An Agent as the registry keeps it: apiVersion, kind, name, namespace and spec."""
    meta = agent.get("metadata") or {}
    return {
        "apiVersion": agent.get("apiVersion"),
        "kind": agent.get("kind"),
        "metadata": {"name": meta.get("name"), "namespace": meta.get("namespace")},
        "spec": agent.get("spec") or {},
    }
