"""Which approval covers a revision, what an approval is bound to, and the checks before one
is recorded (`contract/lifecycle.md` sections 7 and 8). Functions of the store; they write
nothing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent_registry import canonical, derive
from agent_registry.models import Approval, Deployment, Observation, Release, Revision, Tenant
from agent_registry.rules import gates
from agent_registry.rules.authority import widens
from agent_registry.store import Reads

NOT_OBSERVED = "the release has not been observed on the cluster"
TOO_OLD = "the last observation of the release is too old"
NOT_YET = "the cluster does not have this revision yet"


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
