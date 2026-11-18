"""Moving the newest revision forward from what the cluster reports (`contract/lifecycle.md`
section 6). Nothing else activates a revision."""

from __future__ import annotations

from datetime import datetime

from agent_registry import derive
from agent_registry.models import Deployment, Revision
from agent_registry.rules import approval as approvals
from agent_registry.store import Store, Tx

APPROVED_WAITING = "approved; waiting for Ready"


def advance(
    store: Store,
    deployment_id: str,
    *,
    now: datetime,
    max_age_seconds: int,
    actor: str = "registry",
) -> bool:
    """Returns whether anything changed."""
    if store.meta().mode == "recovery":
        return False
    deployment = store.deployment(deployment_id)
    if deployment is None or deployment.kind != "agent" or deployment.authority != "registry":
        return False
    revisions = store.revisions(deployment_id)
    if not revisions:
        return False
    newest = revisions[-1]
    if newest.phase not in ("awaiting-runtime", "pending-approval"):
        return False

    found = approvals.seen(store, deployment, newest, now=now, max_age_seconds=max_age_seconds)
    if not approvals.has_current_object(found, newest):
        return False
    assert found.observation is not None
    document = found.observation.document

    if newest.approval_id is not None:
        return _after_approval(store, deployment, newest, document, now, actor)

    failures = approvals.gate_failures_of(store, deployment, found)
    if failures:
        if (newest.phase, newest.reason) == ("awaiting-runtime", failures[0]):
            return False
        with store.transaction(actor) as tx:
            tx.event("runtime-verdict", deployment_id=deployment_id, detail={"failures": failures})
            tx.set_revision_progress(
                deployment_id, newest.number, phase="awaiting-runtime", reason=failures[0]
            )
        return True

    covered = approvals.covering(store, deployment, newest)
    if covered is not None:
        with store.transaction(actor) as tx:
            seq = _event(
                tx,
                "approval-carried-forward",
                deployment_id,
                {"approval": covered.id, "revision": newest.number},
            )
            tx.set_revision_progress(
                deployment_id,
                newest.number,
                phase="awaiting-runtime",
                approval_id=covered.id,
                reason=APPROVED_WAITING,
                generation=seq,
            )
        return True

    # The event is recorded only when the phase changes (section 6), and a change is never
    # written without an event, so a reason that changes while it waits is not rewritten.
    if newest.phase == "pending-approval":
        return False
    reason = approvals.why_not_covered(store, deployment, newest)
    with store.transaction(actor) as tx:
        tx.event("approval-needed", deployment_id=deployment_id, detail={"reason": reason})
        tx.set_revision_progress(
            deployment_id, newest.number, phase="pending-approval", reason=reason
        )
    return True


def _event(tx: Tx, type: str, deployment_id: str, detail: dict) -> int:
    return tx.event(type, deployment_id=deployment_id, detail=detail).seq


def _after_approval(
    store: Store,
    deployment: Deployment,
    newest: Revision,
    observed: dict,
    now: datetime,
    actor: str,
) -> bool:
    ready = derive.condition(observed, "Ready")
    approved = ((observed.get("spec") or {}).get("approval") or {}).get("approved") is True
    if approved and ready.get("status") == "True":
        with store.transaction(actor) as tx:
            seq = _event(
                tx,
                "revision-activated",
                deployment.id,
                {"revision": newest.number, "pinned": deployment.pinned_revision is not None},
            )
            tx.set_revision_progress(
                deployment.id, newest.number, phase="activated", activated_at=now, reason=""
            )
            if deployment.pinned_revision is None:
                tx.put_deployment(
                    deployment.model_copy(
                        update={"current_revision": newest.number, "generation": seq}
                    )
                )
        return True
    reason = ready.get("reason")
    if not reason:
        return False
    waiting = f"waiting for Ready: {reason}"
    if waiting == newest.reason:
        return False
    with store.transaction(actor) as tx:
        tx.event("runtime-verdict", deployment_id=deployment.id, detail={"ready": str(reason)})
        tx.set_revision_progress(deployment.id, newest.number, reason=waiting)
    return True


def advance_cluster(
    store: Store, cluster: str, *, now: datetime, max_age_seconds: int
) -> list[str]:
    """`advance` for every registry-owned agent of a cluster; the ids that changed."""
    changed = []
    for deployment in store.deployments(kind="agent", cluster=cluster, authority="registry"):
        if advance(store, deployment.id, now=now, max_age_seconds=max_age_seconds):
            changed.append(deployment.id)
    return changed
