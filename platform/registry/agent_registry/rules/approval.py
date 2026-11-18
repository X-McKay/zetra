"""Which approval covers a revision, what an approval is bound to, and the checks before one
is recorded (`contract/lifecycle.md` sections 7 and 8). Functions of the store; they write
nothing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent_registry import canonical, derive
from agent_registry.models import Approval, Deployment, Observation, Release, Revision, Tenant
from agent_registry.rules import Refusal, gates
from agent_registry.rules.authority import widens
from agent_registry.store import Reads, Tx

APPROVED_WAITING = "approved; waiting for Ready"
NOT_OBSERVED = "the release has not been observed on the cluster"
TOO_OLD = "the last observation of the release is too old"
NOT_YET = "the cluster does not have this revision yet"


def owned(reads: Reads, deployment_id: str) -> Deployment:
    """The checks every write route makes first (section 4), through the transaction the
    route is deciding in."""
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
    if deployment.authority == "pull-request":
        raise Refusal(
            "wrong-path",
            f"{deployment.item} is owned by the pull request path; take authority with "
            f"`just agent authority take {deployment.item}` to change it here",
        )
    return deployment


def tenant_of(store: Reads, deployment: Deployment) -> Tenant | None:
    return store.tenant(deployment.cluster, deployment.tenant or "")


def policy_revision(store: Reads, deployment: Deployment) -> str:
    tenant = tenant_of(store, deployment)
    return tenant.policy_revision if tenant else ""


def current_authority(store: Reads, deployment: Deployment) -> dict[str, Any]:
    """The base authority of a revision awaiting approval: the current revision's."""
    if deployment.current_revision is None:
        return canonical.EMPTY_AUTHORITY
    current = store.revision(deployment.id, deployment.current_revision)
    return current.authority if current else canonical.EMPTY_AUTHORITY


def _authority_of_revision(store: Reads, deployment: str, number: int) -> dict[str, Any]:
    found = store.revision(deployment, number)
    return found.authority if found else canonical.EMPTY_AUTHORITY


def registry_approvals(store: Reads, deployment: str) -> list[Approval]:
    """The active approvals made under the registry, oldest first."""
    return [
        a for a in store.approvals(deployment) if a.origin == "registry" and a.state == "active"
    ]


def covering(store: Reads, deployment: Deployment, revision: Revision) -> Approval | None:
    """The newest approval that covers `revision` without a person (section 7)."""
    policy = policy_revision(store, deployment)
    for approval in reversed(registry_approvals(store, deployment.id)):
        if approval.policy_revision != policy:
            continue
        base = _authority_of_revision(store, deployment.id, approval.revision)
        if not widens(base, revision.authority):
            return approval
    return None


def why_not_covered(store: Reads, deployment: Deployment, revision: Revision) -> str:
    """Why no approval covers `revision`, in plain words."""
    active = registry_approvals(store, deployment.id)
    if not active:
        return "no release has been approved under the registry yet"
    newest = active[-1]
    if newest.policy_revision != policy_revision(store, deployment):
        return "the tenant policy changed since the last approval"
    base = _authority_of_revision(store, deployment.id, newest.revision)
    return (widens(base, revision.authority) or ["no approval covers this release"])[0]


@dataclass(frozen=True)
class Seen:
    """What the cluster last reported about a revision's AgentRelease."""

    release: Release
    observation: Observation | None
    state: derive.Freshness
    live: int | None

    @property
    def usable(self) -> bool:
        return self.problem is None

    @property
    def problem(self) -> str | None:
        """Why the gates cannot be read, or None when they can."""
        if self.state in ("missing", "absent"):
            return NOT_OBSERVED
        if self.state == "old":
            return TOO_OLD
        if self.live is None or self.observation is None:
            return NOT_YET
        return None


def seen(
    store: Reads,
    deployment: Deployment,
    revision: Revision,
    *,
    now: datetime,
    max_age_seconds: int,
) -> Seen:
    release = store.release(revision.release_id)
    assert release is not None  # a revision always has its release
    observation = store.observation(
        deployment.cluster, "agentrelease", deployment.namespace, release.name
    )
    state = derive.freshness(observation, now, max_age_seconds)
    live = derive.live_generation(observation.document) if observation else None
    return Seen(release, observation, state, live)


def has_current_object(found: Seen, revision: Revision) -> bool:
    """Fresh, and the object on the cluster was written at or after the revision's last change."""
    return found.state == "fresh" and found.live is not None and found.live >= revision.generation


def problem_for(found: Seen, revision: Revision) -> str | None:
    """Why the observation cannot be used for `revision`, or None when it can."""
    if found.problem:
        return found.problem
    if not has_current_object(found, revision):
        return NOT_YET
    return None


def gate_failures_of(store: Reads, deployment: Deployment, found: Seen) -> list[str]:
    assert found.observation is not None
    return gates.gate_failures(
        found.release.document,
        found.observation.document,
        derive.pin_for(store, deployment.cluster, deployment.tenant, deployment.item),
    )


def evidence(found: Seen) -> dict[str, Any]:
    """What the controller showed when a person approved (section 8)."""
    assert found.observation is not None
    document = found.observation.document
    status = document.get("status") or {}
    signer = status.get("verifiedSigner") or {}
    return {
        "failures": [],
        "observedAt": found.observation.observed_at.isoformat(),
        "signer": signer.get("subject"),
        "attestations": list(signer.get("attestations") or []),
        "policyDigest": status.get("policyDigest"),
        "uid": (document.get("metadata") or {}).get("uid"),
    }


def approve(
    tx: Tx,
    deployment_id: str,
    *,
    approval_id: str,
    revision: int,
    authority_digest: str,
    base_authority_digest: str,
    policy_revision_seen: str,
    ticket: str | None,
    approver: str,
    now: datetime,
    max_age_seconds: int,
) -> None:
    """Record an approval of one revision (section 8). Raises `Refusal`; writes nothing then."""
    deployment = owned(tx, deployment_id)
    if tx.approval(approval_id) is not None:
        raise Refusal("replayed-approval", f"approval {approval_id} was already used")
    target = tx.revision(deployment.id, revision)
    if target is None:
        raise Refusal("not-found", f"{deployment.id} has no revision {revision}")
    newest = max(r.number for r in tx.revisions(deployment.id))
    if target.number != newest:
        raise Refusal("stale-approval", f"revision {target.number} is not the newest ({newest})")
    if target.approval_id is not None:
        raise Refusal("stale-approval", f"revision {target.number} already has an approval")
    if target.phase not in ("awaiting-runtime", "pending-approval"):
        raise Refusal("stale-approval", f"revision {target.number} is {target.phase}, not waiting")
    if authority_digest != target.authority_digest:
        raise Refusal("stale-approval", "the authority is not the one that was shown")
    if policy_revision_seen != policy_revision(tx, deployment):
        raise Refusal("stale-approval", "the tenant policy changed since it was shown")
    base = canonical.authority_digest(current_authority(tx, deployment))
    if base_authority_digest != base:
        raise Refusal("stale-approval", "the current revision changed since it was shown")

    found = seen(tx, deployment, target, now=now, max_age_seconds=max_age_seconds)
    unreadable = problem_for(found, target)
    if unreadable:
        raise Refusal("refused", unreadable)
    failures = gate_failures_of(tx, deployment, found)
    if failures:
        raise Refusal("refused", "the release may not be approved: " + "; ".join(failures))

    seq = tx.event(
        "approved",
        deployment_id=deployment.id,
        detail={
            "approval": approval_id,
            "revision": target.number,
            "authorityDigest": authority_digest,
            "baseAuthorityDigest": base_authority_digest,
            "policyRevision": policy_revision_seen,
            "approvedBy": approver,
        },
    ).seq
    tx.add_approval(
        Approval(
            id=approval_id,
            deployment_id=deployment.id,
            revision=target.number,
            authority_digest=authority_digest,
            base_authority_digest=base_authority_digest,
            policy_revision=policy_revision_seen,
            approved_by=approver,
            approved_at=now,
            ticket=ticket,
            origin="registry",
            evidence=evidence(found),
        )
    )
    for other in registry_approvals(tx, deployment.id):
        if other.id != approval_id:
            tx.set_approval_state(other.id, "superseded")
    tx.set_revision_progress(
        deployment.id,
        target.number,
        phase="awaiting-runtime",
        approval_id=approval_id,
        reason=APPROVED_WAITING,
        generation=seq,
    )
