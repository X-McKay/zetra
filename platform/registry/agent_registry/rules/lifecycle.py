"""Release recording, pin and enable, and moving the newest revision forward from what the
cluster reports (`contract/lifecycle.md` sections 5, 6 and 9). Each route's logic is one
function of a transaction; `advance` decides twice, on plain reads and then in a transaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from agent_registry import canonical, derive
from agent_registry.models import RELEASE_NAME, Revision, revision_id
from agent_registry.rules import Refusal, activation, revocation
from agent_registry.rules import approval as approvals
from agent_registry.rules import revisions as shared
from agent_registry.rules.approval import APPROVED_WAITING, owned
from agent_registry.rules.revisions import OPEN_PHASES, WAITING
from agent_registry.store import Reads, Store, Tx

API_VERSION = "agentplatform.io/v1alpha1"
NO_SUITE = "the release carries no evaluation suite and the current release was admitted by one"

__all__ = [
    "advance",
    "advance_cluster",
    "owned",
    "pin",
    "record_release",
    "rollback",
    "set_enabled",
]


# -- record a release ----------------------------------------------------------------------


def _invalid(message: str) -> Refusal:
    return Refusal("invalid-arguments", message)


def _check_documents(deployment, agent: dict[str, Any], release: dict[str, Any]) -> None:
    if agent.get("kind") != "Agent" or agent.get("apiVersion") != API_VERSION:
        raise _invalid(f"agent: expected an Agent of {API_VERSION}")
    if release.get("kind") != "AgentRelease" or release.get("apiVersion") != API_VERSION:
        raise _invalid(f"agentRelease: expected an AgentRelease of {API_VERSION}")
    agent_meta, release_meta = agent.get("metadata"), release.get("metadata")
    spec = release.get("spec")
    if not isinstance(agent_meta, dict) or not isinstance(release_meta, dict):
        raise _invalid("both documents need metadata")
    if not isinstance(spec, dict):
        raise _invalid("agentRelease: spec is missing")
    if len({agent_meta.get("name"), spec.get("agent"), deployment.item}) != 1:
        raise _invalid(
            "the Agent, the release's spec.agent and the deployment must name the same agent "
            f"({deployment.item})"
        )
    for what, meta in (("agent", agent_meta), ("agentRelease", release_meta)):
        if meta.get("namespace") != deployment.namespace:
            raise _invalid(f"{what}: namespace must be {deployment.namespace}")
    name = release_meta.get("name")
    if not isinstance(name, str) or not RELEASE_NAME.match(name):
        raise _invalid(f"agentRelease: {name!r} is not a release name")
    block = spec.get("approval")
    if block is not None and not isinstance(block, dict):
        raise _invalid("agentRelease: spec.approval must be an object")
    if block and block.get("approved") not in (None, False):
        raise _invalid("a release is never approved in its document; approve it here")


def record_release(
    tx: Tx,
    deployment_id: str,
    agent: dict[str, Any],
    agent_release: dict[str, Any],
    lineage: dict[str, Any],
    *,
    now: datetime,
) -> tuple[str, int, bool]:
    """Record a published release as the next revision (section 5). It replaces nothing that
    runs: the Agent in force is settled afterwards (`contract/delivery.md` section 4).

    Returns the release id, the revision number and whether anything was written.
    """
    deployment = owned(tx, deployment_id)
    _check_documents(deployment, agent, agent_release)

    document = canonical.release_document(agent_release)
    release = derive.agent_release_record(agent_release, recorded_at=now, lineage=lineage)
    revisions = tx.revisions(deployment.id)
    have = tx.release(release.id)
    if have is not None and have.document != document:
        raise Refusal("conflict", f"release {release.name} was recorded with other content")
    for revision in revisions:
        if revision.release_id == release.id:
            return release.id, revision.number, False
    found = revocation.revoked(tx, release)
    if found is not None:
        raise Refusal("refused", f"release {release.name} is revoked ({found.id})")
    tenant = approvals.tenant_of(tx, deployment)
    if tenant is None:
        raise Refusal("refused", f"tenant {deployment.tenant} is not in the feed")

    agent_doc = canonical.agent_document(agent)
    authority = canonical.authority_of(agent_doc)
    requirements = derive.requirements_of(release.id, agent_doc)
    number = max((r.number for r in revisions), default=0) + 1
    seq = tx.event(
        "release-recorded",
        deployment_id=deployment.id,
        detail={
            "release": release.name,
            "revision": number,
            "authorityDigest": canonical.authority_digest(authority),
        },
    ).seq
    tx.add_release(release)
    tx.add_requirements(release.id, requirements)
    tx.add_revision(
        Revision(
            id=revision_id(deployment.id, number),
            deployment_id=deployment.id,
            number=number,
            release_id=release.id,
            origin="registry",
            agent_document=agent_doc,
            authority=authority,
            authority_digest=canonical.authority_digest(authority),
            policy_revision=tenant.policy_revision,
            created_at=now,
            phase="awaiting-runtime",
            reason=WAITING,
            generation=seq,
        )
    )
    tx.add_resolutions(
        revision_id(deployment.id, number),
        derive.resolutions_of(tx, deployment.cluster, deployment.id, number, requirements),
    )
    for earlier in revisions:
        if earlier.phase in OPEN_PHASES:
            tx.set_revision_progress(deployment.id, earlier.number, phase="superseded")
            shared.cancel_open_evaluations(tx, deployment.id, earlier.number, now=now)
    shared.settle_agent(tx, deployment.id, seq)
    return release.id, number, True


# -- pin, enable ---------------------------------------------------------------------------


def set_enabled(tx: Tx, deployment_id: str, enabled: bool, reason: str) -> bool:
    """Turn an agent off or on. Returns whether it changed."""
    deployment = owned(tx, deployment_id)
    if deployment.enabled == enabled:
        return False
    seq = tx.event(
        "enabled" if enabled else "disabled",
        deployment_id=deployment.id,
        detail={"reason": reason},
    ).seq
    tx.put_deployment(deployment.model_copy(update={"enabled": enabled, "generation": seq}))
    return True


def _refuse_target(reads: Reads, deployment, target: Revision, *, now, max_age_seconds) -> None:
    """Raise `Refusal` unless `target` may hold the pointer (`contract/delivery.md` section 7)."""
    number = target.number
    if target.phase != "activated":
        raise Refusal("refused", f"revision {number} has not been activated")
    release = reads.release(target.release_id)
    assert release is not None  # a revision always has its release
    revoked = revocation.revoked(reads, release)
    if revoked is not None:
        raise Refusal("refused", f"release {release.name} is revoked ({revoked.id})")
    wider = approvals.not_in_force(reads, deployment, target)
    if wider is not None:
        raise Refusal(
            "refused", f"revision {number} needs an approval that is no longer in force: {wider}"
        )
    found = approvals.seen(reads, deployment, target, now=now, max_age_seconds=max_age_seconds)
    problem = approvals.problem_for(found, target)
    if problem is not None:
        raise Refusal("refused", problem)
    carries = "declaration" in (release.document.get("spec") or {})
    failures = approvals.gate_failures_in_force(reads, deployment, found, drift=carries)
    if failures:
        raise Refusal("refused", f"revision {number} is not usable: {failures[0]}")
    if carries:
        assert found.observation is not None
        ready = derive.condition(found.observation.document, "Ready")
        if ready.get("status") != "True":
            raise Refusal(
                "refused", f"revision {number} is not Ready: {ready.get('reason') or 'unknown'}"
            )


def _hold(
    tx: Tx,
    deployment_id: str,
    number: int,
    *,
    now: datetime,
    max_age_seconds: int,
    event: str,
    detail: dict[str, Any],
) -> bool:
    """Pin the pointer to a revision that may hold it. False when it already does."""
    deployment = owned(tx, deployment_id)
    target = tx.revision(deployment.id, number)
    if target is None:
        raise Refusal("not-found", f"{deployment.id} has no revision {number}")
    if deployment.pinned_revision == target.number == deployment.current_revision:
        return False
    _refuse_target(tx, deployment, target, now=now, max_age_seconds=max_age_seconds)
    update = {"pinned_revision": target.number, "current_revision": target.number}
    seq = tx.event(event, deployment_id=deployment.id, detail=detail).seq
    tx.put_deployment(deployment.model_copy(update=update | {"generation": seq}))
    shared.settle_agent(tx, deployment.id, seq)
    return True


def pin(
    tx: Tx,
    deployment_id: str,
    revision: int | None,
    *,
    now: datetime,
    max_age_seconds: int,
) -> bool:
    """Hold the pointer on an eligible revision, or (None) let it go. Returns whether it
    changed."""
    if revision is not None:
        return _hold(
            tx,
            deployment_id,
            revision,
            now=now,
            max_age_seconds=max_age_seconds,
            event="pinned",
            detail={"revision": revision},
        )
    deployment = owned(tx, deployment_id)
    if deployment.pinned_revision is None:
        return False
    usable = [
        r.number
        for r in tx.revisions(deployment.id)
        if r.phase == "activated" and not _revoked(tx, r)
    ]
    seq = tx.event(
        "unpinned", deployment_id=deployment.id, detail={"was": deployment.pinned_revision}
    ).seq
    tx.put_deployment(
        deployment.model_copy(
            update={
                "pinned_revision": None,
                "current_revision": max(usable, default=None),
                "generation": seq,
            }
        )
    )
    shared.settle_agent(tx, deployment.id, seq)
    return True


def _revoked(reads: Reads, revision: Revision) -> bool:
    release = reads.release(revision.release_id)
    return release is not None and revocation.revoked(reads, release) is not None


def rollback(
    tx: Tx,
    deployment_id: str,
    revision: int | None,
    reason: str,
    *,
    now: datetime,
    max_age_seconds: int,
) -> bool:
    """Return to an earlier release: a pin of the target, recorded as `rolled-back`. With no
    revision the target is the newest activated one below the current, whose release is not
    revoked. Returns whether it changed."""
    deployment = owned(tx, deployment_id)
    if revision is None:
        below = deployment.current_revision
        earlier = [
            r.number
            for r in tx.revisions(deployment.id)
            if r.phase == "activated"
            and (below is None or r.number < below)
            and not _revoked(tx, r)
        ]
        if not earlier:
            raise Refusal("refused", "there is no earlier release to return to")
        revision = max(earlier)
    return _hold(
        tx,
        deployment.id,
        revision,
        now=now,
        max_age_seconds=max_age_seconds,
        event="rolled-back",
        detail={"from": deployment.current_revision, "to": revision, "reason": reason},
    )


# -- advance -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Plan:
    """What one decision says to write. `action` is one of: `progress` (an event and the
    revision's progress), `carry` (the same, with the revision's generation raised), `activate`,
    `evaluate` (D8) and `refuse`."""

    revision: int
    event: str
    detail: dict[str, Any]
    progress: dict[str, Any] = field(default_factory=dict)
    action: str = "progress"


def _decide(reads: Reads, deployment_id: str, now: datetime, max_age_seconds: int) -> _Plan | None:
    """What `advance` would write, from `reads` alone. None: nothing."""
    if reads.meta().mode == "recovery":
        return None
    deployment = reads.deployment(deployment_id)
    if deployment is None or deployment.kind != "agent" or deployment.authority != "registry":
        return None
    revisions = reads.revisions(deployment_id)
    if not revisions:
        return None
    newest = revisions[-1]
    if newest.phase not in ("awaiting-runtime", "pending-approval"):
        return None

    found = approvals.seen(reads, deployment, newest, now=now, max_age_seconds=max_age_seconds)
    if not approvals.has_current_object(found, newest):
        return None
    assert found.observation is not None
    observed = found.observation.document

    if newest.approval_id is not None:
        ready = derive.condition(observed, "Ready")
        approved = ((observed.get("spec") or {}).get("approval") or {}).get("approved") is True
        if approved and ready.get("status") == "True":
            return _decide_ready(reads, deployment, newest, found.release)
        reason = ready.get("reason")
        if not reason or f"waiting for Ready: {reason}" == newest.reason:
            return None
        return _Plan(
            newest.number,
            "runtime-verdict",
            {"ready": str(reason)},
            {"reason": f"waiting for Ready: {reason}"},
        )

    failures = approvals.gate_failures_of(reads, deployment, found, newest)
    if failures:
        if (newest.phase, newest.reason) == ("awaiting-runtime", failures[0]):
            return None
        return _Plan(
            newest.number,
            "runtime-verdict",
            {"failures": failures},
            {"phase": "awaiting-runtime", "reason": failures[0]},
        )

    covered = approvals.covering(reads, deployment, newest)
    if covered is not None:
        return _Plan(
            newest.number,
            "approval-carried-forward",
            {"approval": covered.id, "revision": newest.number},
            {"phase": "awaiting-runtime", "approval_id": covered.id, "reason": APPROVED_WAITING},
            action="carry",
        )

    reason = approvals.why_not_covered(reads, deployment, newest)
    if (newest.phase, newest.reason) == ("pending-approval", reason):
        return None
    return _Plan(
        newest.number,
        "approval-needed",
        {"reason": reason},
        {"phase": "pending-approval", "reason": reason},
    )


def _decide_ready(reads: Reads, deployment, newest: Revision, release) -> _Plan:
    """D6 to D8: an approved revision the cluster shows Ready."""
    if activation.required(release):
        return _Plan(newest.number, "evaluation-requested", {}, action="evaluate")
    if activation.admission(reads, deployment.id, deployment.current_revision) is not None:
        return _Plan(
            newest.number,
            "revision-refused",
            {},
            {"reason": NO_SUITE},
            action="refuse",
        )
    return _Plan(newest.number, "revision-activated", {}, action="activate")


def _apply(tx: Tx, deployment_id: str, plan: _Plan, now: datetime) -> int:
    """Write a plan. Returns the sequence number of the last event."""
    if plan.action == "activate":
        return activation.activate(tx, deployment_id, plan.revision, now=now, evaluation=None)
    if plan.action == "refuse":
        return activation.refuse(tx, deployment_id, plan.revision, plan.progress["reason"], now=now)
    if plan.action == "evaluate":
        revision = tx.revision(deployment_id, plan.revision)
        release = tx.release(revision.release_id)
        assert release is not None
        return activation.request(tx, deployment_id, revision, release, now=now)[1]
    seq = tx.event(plan.event, deployment_id=deployment_id, detail=plan.detail).seq
    progress = dict(plan.progress)
    if plan.action == "carry":
        progress["generation"] = seq
    tx.set_revision_progress(deployment_id, plan.revision, **progress)
    return seq


def advance(
    store: Store,
    deployment_id: str,
    *,
    now: datetime,
    max_age_seconds: int,
    actor: str = "registry",
) -> bool:
    """Move the newest revision forward from what the cluster reports. Returns whether
    anything changed.

    Decides on plain reads first, which nearly always finds nothing; only then opens a
    transaction and decides again through it, which is the decision that counts.
    """
    if _decide(store, deployment_id, now, max_age_seconds) is None:
        return False
    with store.transaction(actor) as tx:
        plan = _decide(tx, deployment_id, now, max_age_seconds)
        if plan is None:
            return False
        seq = _apply(tx, deployment_id, plan, now)
        shared.settle_agent(tx, deployment_id, seq)
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
