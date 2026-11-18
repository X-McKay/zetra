"""What follows from a deployment's revisions: the Agent in force and the evaluations still
open (`contract/delivery.md` sections 4 and 5).

`settle_agent` and `cancel_open_evaluations` are called by the revocation rules as well as by
the lifecycle rules; their signatures are fixed. Import `rules.revocation` as a module here,
not its names: the two modules call each other.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agent_registry import derive
from agent_registry.models import Deployment, Resolution, Revision, revision_id
from agent_registry.rules import revocation
from agent_registry.store import Reads, Tx

WAITING = "waiting for the controller's verdict"

# A revision in one of these phases is the deployment's candidate when it is the newest.
OPEN_PHASES = ("awaiting-runtime", "pending-approval", "evaluating")
OPEN_EVALUATIONS = ("pending", "claimed")


def candidate(revisions: list[Revision]) -> Revision | None:
    """The newest revision, while it has not been activated, refused or superseded."""
    if revisions and revisions[-1].phase in OPEN_PHASES:
        return revisions[-1]
    return None


def _ever_revoked(reads: Reads, revisions: list[Revision]) -> bool:
    for release_id in {r.release_id for r in revisions}:
        release = reads.release(release_id)
        if release is not None and revocation.revoked(reads, release) is not None:
            return True
    return False


def agent_in_force(reads: Reads, deployment: Deployment) -> dict[str, Any]:
    """The Agent document the cluster should be given (section 4), from `reads` alone."""
    revisions = reads.revisions(deployment.id)
    stored = deployment.document.get("agent") or {}
    newest = candidate(revisions)
    if newest is not None and newest.approval_id is not None:
        return newest.agent_document
    if deployment.current_revision is not None:
        current = reads.revision(deployment.id, deployment.current_revision)
        return current.agent_document if current is not None else stored
    if revisions and not _ever_revoked(reads, revisions):
        return revisions[-1].agent_document
    return stored


def settle_agent(tx: Tx, deployment_id: str, seq: int | None) -> bool:
    """Store the Agent in force when it is not the stored one. Returns whether it changed.

    Call it last in every transaction that could change the choice. `seq` is the number of
    the event the transaction already wrote; the deployment's generation becomes that. With
    None, the transaction wrote no event of its own, and `agent-settled` is written here
    when there is a change.
    """
    deployment = tx.deployment(deployment_id)
    if deployment is None or deployment.kind != "agent" or deployment.authority != "registry":
        return False
    chosen = agent_in_force(tx, deployment)
    if chosen == (deployment.document.get("agent") or {}):
        return False
    if seq is None:
        seq = tx.event("agent-settled", deployment_id=deployment.id, detail={}).seq
    tx.put_deployment(
        deployment.model_copy(
            update={"document": {**deployment.document, "agent": chosen}, "generation": seq}
        )
    )
    return True


def cancel_open_evaluations(tx: Tx, deployment_id: str, number: int, *, now: datetime) -> list[str]:
    """Cancel the `pending` and `claimed` evaluations of one revision. Returns their ids."""
    cancelled = []
    for evaluation in tx.evaluations(deployment_id):
        if evaluation.revision == number and evaluation.state in OPEN_EVALUATIONS:
            tx.set_evaluation_progress(evaluation.id, state="cancelled", finished_at=now)
            cancelled.append(evaluation.id)
    return cancelled


def _resolution_key(row: Resolution) -> tuple[str, str, str | None, str | None, str]:
    return (
        row.type,
        row.target,
        row.resolved_deployment,
        row.resolved_release,
        repr(sorted(row.detail.items())),
    )


def _changes(old: list[Resolution], new: list[Resolution]) -> list[dict[str, Any]]:
    """What differs between two sets of resolutions, one entry per requirement."""
    before = {(r.type, r.target): r for r in old}
    found = []
    for row in new:
        was = before.get((row.type, row.target))
        if was is None or _resolution_key(was) != _resolution_key(row):
            found.append(
                {
                    "type": row.type,
                    "target": row.target,
                    "from": was.resolved_deployment if was else None,
                    "to": row.resolved_deployment,
                }
            )
    return found


def re_resolve(tx: Tx, deployment_id: str, *, now: datetime) -> int | None:
    """Record a new revision of the newest revision's release when what its requirements
    resolve to has changed (section 5.1). Returns the new revision's number, or None.

    The new revision goes through the phases like any other: its approval is carried forward
    when it is covered, and its evaluation is required again. Nothing is done in recovery, on
    the pull request path, or for a revoked release (the revocation is the record).
    """
    deployment = tx.deployment(deployment_id)
    if (
        tx.meta().mode == "recovery"
        or deployment is None
        or deployment.kind != "agent"
        or deployment.authority != "registry"
    ):
        return None
    revisions = tx.revisions(deployment.id)
    if not revisions:
        return None
    newest = revisions[-1]
    release = tx.release(newest.release_id)
    if release is None or revocation.revoked(tx, release) is not None:
        return None
    tenant = tx.tenant(deployment.cluster, deployment.tenant or "")
    if tenant is None:
        return None
    resolutions = derive.resolutions_of(
        tx, deployment.cluster, deployment.id, newest.number + 1, tx.requirements(release.id)
    )
    changes = _changes(tx.resolutions(newest.id), resolutions)
    if not changes:
        return None

    number = newest.number + 1
    seq = tx.event(
        "revision-recorded",
        deployment_id=deployment.id,
        detail={
            "release": release.name,
            "revision": number,
            "from": newest.number,
            "changed": changes,
        },
    ).seq
    tx.add_revision(
        Revision(
            id=revision_id(deployment.id, number),
            deployment_id=deployment.id,
            number=number,
            release_id=release.id,
            origin="registry",
            agent_document=newest.agent_document,
            authority=newest.authority,
            authority_digest=newest.authority_digest,
            policy_revision=tenant.policy_revision,
            created_at=now,
            phase="awaiting-runtime",
            reason=WAITING,
            generation=seq,
        )
    )
    tx.add_resolutions(revision_id(deployment.id, number), resolutions)
    if newest.phase in OPEN_PHASES:
        tx.set_revision_progress(deployment.id, newest.number, phase="superseded")
        cancel_open_evaluations(tx, deployment.id, newest.number, now=now)
    settle_agent(tx, deployment.id, seq)
    return number
