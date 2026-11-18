"""Taking authority from the pull request path and giving it back (`contract/projection.md`
section 7). Each is a function of the transaction the route opened."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agent_registry import derive
from agent_registry.models import Deployment, Observation
from agent_registry.rules import Refusal, projection
from agent_registry.store import Reads, Tx

NOT_FRESH = {
    "missing": "has never been observed",
    "old": "was last observed too long ago",
    "absent": "is not on the cluster",
}


def _agent_deployment(reads: Reads, deployment_id: str) -> Deployment:
    meta = reads.meta()
    if meta.mode == "recovery":
        raise Refusal(
            "in-recovery",
            f"the registry is in recovery ({meta.mode_reason}); nothing is written until "
            "recovery is left",
        )
    deployment = reads.deployment(deployment_id)
    if deployment is None or deployment.kind != "agent":
        raise Refusal("not-found", f"there is no agent deployment {deployment_id}")
    return deployment


def _spec_without_approval(observation: Observation) -> dict[str, Any]:
    spec = (observation.document.get("spec") or {}).copy()
    spec.pop("approval", None)
    return spec


def differences(reads: Reads, deployment: Deployment, *, now: datetime, max_age: int) -> list[str]:
    """Every way the registry's records differ from what the cluster last reported."""
    reasons: list[str] = []

    def seen(kind: str, name: str) -> Observation | None:
        found = reads.observation(deployment.cluster, kind, deployment.namespace, name)
        state = derive.freshness(found, now, max_age)
        if state != "fresh":
            reasons.append(f"{kind} {name} {NOT_FRESH[state]}")
            return None
        return found

    live = seen("agent", deployment.item)
    if live is not None:
        expected = deployment.document["agent"].get("spec") or {}
        if (live.document.get("spec") or {}) != expected:
            reasons.append(f"the spec of agent {deployment.item} on the cluster differs")

    names = set()
    for revision in reads.revisions(deployment.id):
        release = reads.release(revision.release_id)
        assert release is not None
        names.add(release.name)
        found = seen("agentrelease", release.name)
        if found is None:
            continue
        if _spec_without_approval(found) != (release.document.get("spec") or {}):
            reasons.append(f"the spec of agentrelease {release.name} on the cluster differs")
        shown = (found.document.get("spec") or {}).get("approval")
        block = projection.approval_block(reads, revision)
        if revision.approval_id is None:
            differs = bool(shown) and shown.get("approved") is True
        else:
            differs = shown != block
        if differs:
            reasons.append(f"the approval of agentrelease {release.name} on the cluster differs")

    for other in reads.observations(
        deployment.cluster, subject_kind="agentrelease", namespace=deployment.namespace
    ):
        spec = other.document.get("spec") or {}
        if other.present and spec.get("agent") == deployment.item and other.name not in names:
            reasons.append(
                f"agentrelease {other.name} is on the cluster and is not a revision of "
                f"{deployment.item}"
            )
    return reasons


def take(tx: Tx, deployment_id: str, *, now: datetime, max_age_seconds: int) -> bool:
    """Move an agent to the registry. Returns whether it changed."""
    deployment = _agent_deployment(tx, deployment_id)
    if deployment.authority == "registry":
        return False
    reasons = differences(tx, deployment, now=now, max_age=max_age_seconds)
    if reasons:
        raise Refusal(
            "conflict",
            f"the registry's records of {deployment.item} are not what the cluster holds: "
            + "; ".join(reasons),
        )
    seq = tx.event("authority-taken", deployment_id=deployment.id).seq
    tx.put_deployment(deployment.model_copy(update={"authority": "registry", "generation": seq}))
    for revision in tx.revisions(deployment.id):
        tx.set_revision_progress(deployment.id, revision.number, generation=seq)
    return True


def give_back(tx: Tx, deployment_id: str) -> bool:
    """Move an agent back to the pull request path. Returns whether it changed."""
    deployment = _agent_deployment(tx, deployment_id)
    if deployment.authority == "pull-request":
        return False
    tx.event("authority-returned", deployment_id=deployment.id)
    tx.put_deployment(deployment.model_copy(update={"authority": "pull-request"}))
    return True


def documents_to_commit(
    reads: Reads, deployment: Deployment
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The Agent and the AgentReleases of section 3, without annotations, in revision order."""
    objects = projection.objects_of(reads, deployment)
    agent, *releases = (projection.without_annotations(o.document) for o in objects)
    return agent, releases
