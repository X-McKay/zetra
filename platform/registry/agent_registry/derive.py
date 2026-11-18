"""Records derived from documents. The importer and the write rules both build releases,
requirements and resolutions with these functions, so the two cannot disagree."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from agent_registry import canonical
from agent_registry.canonical import release_document
from agent_registry.config import ANNOTATION_PREFIX
from agent_registry.models import (
    Observation,
    Release,
    Requirement,
    Resolution,
    TrustedRepository,
    release_id,
    release_slug,
    revision_id,
)
from agent_registry.store import Reads

IMAGE_DIGEST = re.compile(r"@(sha256:[a-f0-9]{64})\Z")

Freshness = Literal["fresh", "old", "absent", "missing"]


def image_digest(image: str) -> str:
    """`sha256:<hex>` of an image reference pinned by digest, or "" when it is pinned by tag."""
    found = IMAGE_DIGEST.search(image or "")
    return found.group(1) if found else ""


def sha(value: Any) -> str:
    """`sha256:<hex>` of the canonical form: the shape every digest of a contract or a model
    release takes."""
    return "sha256:" + canonical.digest(value)


def contract_record(contract: dict[str, Any] | None) -> dict[str, Any] | None:
    """A tool contract file as the registry keeps it (`contract/dependencies.md` section 2.3),
    or None when the server has no contract. A tool's digest covers its four other fields; the
    contract's covers the map of tool name to tool digest."""
    if contract is None:
        return None
    tools = {}
    for tool in contract["tools"]:
        fields = {
            "description": tool["description"],
            "effect": tool["effect"],
            "inputSchema": tool["inputSchema"],
            "outputSchema": tool.get("outputSchema"),
        }
        tools[tool["name"]] = fields | {"digest": sha(fields)}
    return {
        "digest": sha({name: tool["digest"] for name, tool in tools.items()}),
        "image": contract["image"],
        "configDigest": contract["configDigest"],
        "tools": tools,
    }


def engine_release(document: dict[str, Any]) -> str:
    """The name of the release a model engine serves: the one its document names, else the one
    adopted for its served name."""
    return document.get("release") or release_slug(document["servedName"])


def agent_release_record(
    agent_release: dict[str, Any],
    *,
    recorded_at: datetime,
    lineage: dict[str, Any] | None = None,
) -> Release:
    """The release record of an AgentRelease document. `spec.approval` is not part of it."""
    document = release_document(agent_release)
    item = document["spec"].get("agent") or ""
    name = document["metadata"]["name"] or ""
    return Release(
        id=release_id("agent", item, name),
        kind="agent",
        item=item,
        name=name,
        digest=image_digest(document["spec"].get("image") or ""),
        origin="built",
        lineage=lineage or {},
        document=document,
        recorded_at=recorded_at,
    )


def requirements_of(
    rid: str, agent_document: dict[str, Any], skills: list[dict[str, Any]] | None = None
) -> list[Requirement]:
    """What a release needs, read from the Agent document published with it (milestone 1) and
    the release's `spec.skills`.

    One row per tool backend, model backend, requested effect and skill, sorted by type and
    then target. A skill row names its one digest.
    """
    capabilities = (agent_document.get("spec") or {}).get("capabilities") or {}
    rows = [
        Requirement(
            release_id=rid,
            type="tool",
            target=str(t.get("backend")),
            names=sorted(t.get("tools") or []),
            detail={"effect": t.get("effect"), "delegation": t.get("delegation")},
        )
        for t in capabilities.get("mcp") or []
    ] + [
        Requirement(
            release_id=rid,
            type="model",
            target=str(m.get("backend")),
            names=sorted(m.get("models") or []),
        )
        for m in capabilities.get("models") or []
    ]
    rows += [
        Requirement(release_id=rid, type="effect", target=effect)
        for effect in sorted(set(capabilities.get("effects") or []))
    ] + [
        Requirement(release_id=rid, type="skill", target=str(k.get("name")), names=[k["digest"]])
        for k in skills or []
    ]
    return sorted(rows, key=lambda r: (r.type, r.target))


def resolutions_of(
    store: Reads, cluster: str, deployment: str, number: int, requirements: list[Requirement]
) -> list[Resolution]:
    """What each requirement resolves to on a cluster today.

    A tool backend resolves to the tool server deployment that is registered under that
    backend name. A model backend resolves to the model deployment an alias points at.
    Anything else resolves to nothing (`resolved_deployment` None), which is a fact worth
    showing and not an error.
    """
    servers = {
        d.document.get("backend"): d
        for d in store.deployments(kind="tool-server", cluster=cluster)
        if d.document.get("registered")
    }
    aliases = {a.backend: a for a in store.aliases(cluster)}
    rows = []
    for requirement in requirements:
        target, detail, resolved_release = None, {}, None
        if requirement.type == "tool" and requirement.target in servers:
            target = servers[requirement.target].id
            document = servers[requirement.target].document
            detail = {"image": document.get("image")}
            if document.get("contract"):
                tools = document["contract"]["tools"]
                detail["contract"] = sha(
                    {name: (tools.get(name) or {}).get("digest") for name in requirement.names}
                )
        elif requirement.type == "model" and requirement.target in aliases:
            alias = aliases[requirement.target]
            target = alias.deployment_id
            model = store.deployment(alias.deployment_id)
            detail = {
                "alias": alias.alias,
                "servedName": model.document.get("servedName") if model else None,
            }
            if model and model.document.get("release"):
                detail["release"] = engine_release(model.document)
                resolved_release = release_id("model", model.item, detail["release"])
        rows.append(
            Resolution(
                revision_id=revision_id(deployment, number),
                type=requirement.type,
                target=requirement.target,
                resolved_deployment=target,
                resolved_release=resolved_release,
                detail=detail,
            )
        )
    return rows


def freshness(observation: Observation | None, now: datetime, max_age_seconds: int) -> Freshness:
    """How far an observation can be trusted.

    missing: nothing was ever reported. old: the last report is older than the limit.
    absent: a recent report says the subject is not on the cluster. fresh: recent and present.
    """
    if observation is None:
        return "missing"
    if (now - observation.observed_at).total_seconds() > max_age_seconds:
        return "old"
    return "fresh" if observation.present else "absent"


def age_seconds(observation: Observation | None, now: datetime) -> int | None:
    if observation is None:
        return None
    return max(0, int((now - observation.observed_at).total_seconds()))


def condition(document: dict[str, Any], type: str) -> dict[str, Any]:
    """The status condition of that type in an observed object, or {} when it has none."""
    for entry in (document.get("status") or {}).get("conditions") or []:
        if entry.get("type") == type:
            return entry
    return {}


def live_generation(document: dict[str, Any]) -> int | None:
    """The registry generation an observed object was last written at: its
    `registry.agentplatform.io/generation` annotation. None when the registry never wrote it."""
    annotations = (document.get("metadata") or {}).get("annotations") or {}
    try:
        return int(annotations[ANNOTATION_PREFIX + "generation"])
    except (KeyError, TypeError, ValueError):
        return None


def pin_for(store: Reads, cluster: str, tenant: str | None, agent: str) -> TrustedRepository | None:
    """The trusted repository that lists an agent for a cluster and tenant."""
    for repository in store.trusted_repositories():
        if (
            repository.cluster == cluster
            and repository.tenant == tenant
            and agent in repository.agents
        ):
            return repository
    return None
