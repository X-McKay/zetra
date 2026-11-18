"""What a cluster is given (`contract/projection.md` sections 1 to 4): the objects of a
registry-owned agent, and the workloads a provisioner observes. Functions of plain reads."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from agent_registry.config import ANNOTATION_PREFIX
from agent_registry.models import Deployment, Revision
from agent_registry.rules import revocation
from agent_registry.store import Reads


@dataclass(frozen=True)
class ProjectedObject:
    kind: str  # Agent or AgentRelease
    namespace: str
    name: str
    generation: int
    document: dict[str, Any]


@dataclass(frozen=True)
class Observed:
    """A workload the provisioner reports on and never writes."""

    subject_kind: str  # tool-server or model-engine
    name: str
    namespace: str
    workload: dict[str, Any]
    served_name_from: dict[str, str] | None


def approval_block(reads: Reads, revision: Revision) -> dict[str, Any]:
    """`spec.approval` of a revision's AgentRelease (section 3). Follows `approval_id`
    whatever the approval's state."""
    approval = reads.approval(revision.approval_id) if revision.approval_id else None
    if approval is None:
        return {"approved": False}
    if approval.origin != "registry":
        return copy.deepcopy(approval.evidence["approval"])
    block: dict[str, Any] = {
        "approved": True,
        "approvedBy": approval.approved_by,
        "approvedAt": approval.approved_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if approval.ticket:
        block["ticket"] = approval.ticket
    return block


def agent_spec(deployment: Deployment) -> dict[str, Any]:
    """The projected spec of the Agent: `enabled` is the deployment's and the document's."""
    spec = copy.deepcopy(deployment.document["agent"].get("spec") or {})
    spec["enabled"] = deployment.enabled and spec.get("enabled", True)
    return spec


def _annotations(deployment: Deployment, generation: int) -> dict[str, str]:
    return {
        ANNOTATION_PREFIX + "deployment": deployment.id,
        ANNOTATION_PREFIX + "cluster": deployment.cluster,
        ANNOTATION_PREFIX + "generation": str(generation),
    }


def _meta(document: dict[str, Any], deployment: Deployment, fallback: str) -> dict[str, str]:
    meta = document.get("metadata") or {}
    return {"name": meta.get("name") or fallback, "namespace": meta.get("namespace") or ""}


def agent_object(reads: Reads, deployment: Deployment) -> ProjectedObject:
    agent = deployment.document["agent"]
    meta = _meta(agent, deployment, deployment.item)
    annotations = _annotations(deployment, deployment.generation)
    current = (
        reads.revision(deployment.id, deployment.current_revision)
        if deployment.current_revision is not None
        else None
    )
    release = reads.release(current.release_id) if current else None
    if release is not None:
        annotations[ANNOTATION_PREFIX + "current-release"] = release.name
    return ProjectedObject(
        kind="Agent",
        namespace=meta["namespace"],
        name=meta["name"],
        generation=deployment.generation,
        document={
            "apiVersion": agent.get("apiVersion"),
            "kind": "Agent",
            "metadata": {**meta, "annotations": annotations},
            "spec": agent_spec(deployment),
        },
    )


def release_approval_block(reads: Reads, deployment: Deployment, release_id: str) -> dict[str, Any]:
    """What the one AgentRelease object of a release shows as its approval: the block of the
    newest revision of the release that has an approval, and `{"approved": false}` when none
    has or when the release is revoked (`contract/delivery.md` section 5.1)."""
    release = reads.release(release_id)
    if release is None or revocation.revoked(reads, release) is not None:
        return {"approved": False}
    for revision in reversed(reads.revisions(deployment.id)):
        if revision.release_id == release_id and revision.approval_id is not None:
            return approval_block(reads, revision)
    return {"approved": False}


def release_object(reads: Reads, deployment: Deployment, revision: Revision) -> ProjectedObject:
    """The AgentRelease of `revision`'s release, where `revision` is the newest revision of it
    (`contract/delivery.md` section 5.1). The approval block is the one of the newest revision
    of the release that has an approval, and `{"approved": false}` when none has or when the
    release is revoked."""
    release = reads.release(revision.release_id)
    assert release is not None  # a revision always has its release
    document = release.document
    meta = _meta(document, deployment, release.name)
    annotations = _annotations(deployment, revision.generation)
    annotations[ANNOTATION_PREFIX + "revision"] = str(revision.number)
    revoked = revocation.revoked(reads, release)
    if revoked is not None:
        annotations[ANNOTATION_PREFIX + "revoked"] = revoked.id
    block = release_approval_block(reads, deployment, revision.release_id)
    spec = {**copy.deepcopy(document.get("spec") or {}), "approval": block}
    return ProjectedObject(
        kind="AgentRelease",
        namespace=meta["namespace"],
        name=meta["name"],
        generation=revision.generation,
        document={
            "apiVersion": document.get("apiVersion"),
            "kind": "AgentRelease",
            "metadata": {**meta, "annotations": annotations},
            "spec": spec,
        },
    )


def objects_of(reads: Reads, deployment: Deployment) -> list[ProjectedObject]:
    """The Agent first, then one AgentRelease per release, in ascending number of the newest
    revision of each."""
    newest: dict[str, Revision] = {}
    for revision in reads.revisions(deployment.id):
        newest[revision.release_id] = revision
    return [
        agent_object(reads, deployment),
        *(
            release_object(reads, deployment, r)
            for r in sorted(newest.values(), key=lambda r: r.number)
        ),
    ]


def without_annotations(document: dict[str, Any]) -> dict[str, Any]:
    """The document to commit to git: the same, with no registry annotations."""
    result = copy.deepcopy(document)
    result["metadata"].pop("annotations", None)
    return result


def registry_owned(reads: Reads, cluster: str) -> list[Deployment]:
    return sorted(
        reads.deployments(kind="agent", cluster=cluster, authority="registry"), key=lambda d: d.id
    )


def namespaces_of(reads: Reads, cluster: str) -> list[str]:
    return sorted({t.namespace for t in reads.tenants(cluster)})


def observe_of(reads: Reads, cluster: str) -> list[Observed]:
    """The tool servers and model engines of a cluster, from their deployments' documents."""
    found = []
    for kind, subject_kind in (("tool-server", "tool-server"), ("model", "model-engine")):
        for deployment in sorted(reads.deployments(kind=kind, cluster=cluster), key=lambda d: d.id):
            document = deployment.document
            namespace = document.get("namespace") or deployment.namespace
            found.append(
                Observed(
                    subject_kind=subject_kind,
                    name=deployment.item,
                    namespace=namespace,
                    workload={**(document.get("workload") or {}), "namespace": namespace},
                    served_name_from=(document.get("servedNameFrom") if kind == "model" else None),
                )
            )
    return found
