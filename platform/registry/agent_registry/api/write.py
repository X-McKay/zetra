"""The write routes for a registry-owned agent (package 1.4a). Caller: a signed-in person
with `registry-operator`.

Every route here refuses, in this order and before anything else: `in-recovery` (the
`writable` dependency); `not-found` when the deployment does not exist or is not an agent;
`wrong-path` when the agent is owned by the pull request path. A refusal writes nothing.
The rules are in `contract/lifecycle.md` and the logic lives in `agent_registry/rules/`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from agent_registry import canonical, derive
from agent_registry.api import schemas as s
from agent_registry.api.auth import Principal, operator
from agent_registry.api.deps import get_settings, get_store, now, writable
from agent_registry.api.errors import ApiError
from agent_registry.config import Settings
from agent_registry.models import (
    RELEASE_NAME,
    Approval,
    Deployment,
    Revision,
    revision_id,
)
from agent_registry.rules import approval as approvals
from agent_registry.rules.authority import widens
from agent_registry.store import Store

API_VERSION = "agentplatform.io/v1alpha1"
WAITING = "waiting for the controller's verdict"
APPROVED_WAITING = "approved; waiting for Ready"

# The caller is checked before the mode, so that an unknown caller learns nothing.
router = APIRouter(
    prefix="/v1", tags=["write"], dependencies=[Depends(operator), Depends(writable)]
)

StoreDep = Annotated[Store, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
Operator = Annotated[Principal, Depends(operator)]
Now = Annotated[datetime, Depends(now)]


def _owned(store: Store, deployment_id: str) -> Deployment:
    """The checks every write route makes after `in-recovery` (section 4)."""
    deployment = store.deployment(deployment_id)
    if deployment is None or deployment.kind != "agent":
        raise ApiError("not-found", f"there is no agent deployment {deployment_id}")
    if deployment.authority == "pull-request":
        raise ApiError(
            "wrong-path",
            f"{deployment.item} is owned by the pull request path; take authority with "
            f"`just agent authority take {deployment.item}` to change it here",
        )
    return deployment


def _revision(store: Store, deployment: Deployment, number: int) -> Revision:
    found = store.revision(deployment.id, number)
    if found is None:
        raise ApiError("not-found", f"{deployment.id} has no revision {number}")
    return found


def _invalid(message: str) -> ApiError:
    return ApiError("invalid-arguments", message)


def _check_documents(deployment: Deployment, body: s.RecordReleaseIn) -> None:
    agent, release = body.agent, body.agent_release
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
    names = {agent_meta.get("name"), spec.get("agent"), deployment.item}
    if len(names) != 1:
        raise _invalid(
            f"the Agent, the release's spec.agent and the deployment must name the same agent "
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


@router.post("/deployments/{deployment_id}/releases", response_model=s.RecordReleaseOut)
def record_release(
    deployment_id: str, body: s.RecordReleaseIn, store: StoreDep, who: Operator, at: Now
) -> s.RecordReleaseOut:
    """Record a published release and create the next revision, projected unapproved."""
    deployment = _owned(store, deployment_id)
    _check_documents(deployment, body)

    document = canonical.release_document(body.agent_release)
    release = derive.agent_release_record(body.agent_release, recorded_at=at, lineage=body.lineage)
    for revision in store.revisions(deployment.id):
        if revision.release_id == release.id:
            have = store.release(release.id)
            if have is not None and have.document == document:
                return s.RecordReleaseOut(release=have, revision=revision, created=False)
    have = store.release(release.id)
    if have is not None and have.document != document:
        raise ApiError("conflict", f"release {release.name} was recorded with other content")
    tenant = approvals.tenant_of(store, deployment)
    if tenant is None:
        raise ApiError("refused", f"tenant {deployment.tenant} is not in the feed")

    agent = canonical.agent_document(body.agent)
    authority = canonical.authority_of(agent)
    requirements = derive.requirements_of(release.id, agent)
    number = max((r.number for r in store.revisions(deployment.id)), default=0) + 1
    with store.transaction(who.actor) as tx:
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
                agent_document=agent,
                authority=authority,
                authority_digest=canonical.authority_digest(authority),
                policy_revision=tenant.policy_revision,
                created_at=at,
                phase="awaiting-runtime",
                reason=WAITING,
                generation=seq,
            )
        )
        tx.add_resolutions(
            revision_id(deployment.id, number),
            derive.resolutions_of(tx, deployment.cluster, deployment.id, number, requirements),
        )
        for earlier in tx.revisions(deployment.id):
            if earlier.number != number and earlier.phase in (
                "awaiting-runtime",
                "pending-approval",
            ):
                tx.set_revision_progress(deployment.id, earlier.number, phase="superseded")
        tx.put_deployment(
            deployment.model_copy(
                update={"document": {**deployment.document, "agent": agent}, "generation": seq}
            )
        )
    return s.RecordReleaseOut(
        release=store.release(release.id),
        revision=store.revision(deployment.id, number),
        created=True,
    )


@router.get(
    "/deployments/{deployment_id}/revisions/{number}/approval",
    response_model=s.ApprovalPreviewOut,
)
def approval_preview(
    deployment_id: str,
    number: int,
    store: StoreDep,
    settings: SettingsDep,
    who: Operator,
    at: Now,
) -> s.ApprovalPreviewOut:
    """What approving this revision would bind to, and whether it can be approved now."""
    deployment = _owned(store, deployment_id)
    revision = _revision(store, deployment, number)
    found = approvals.seen(
        store, deployment, revision, now=at, max_age_seconds=settings.observation_max_age_seconds
    )
    unreadable = approvals.problem_for(found, revision)
    failures = [unreadable] if unreadable else approvals.gate_failures_of(store, deployment, found)
    base = approvals.current_authority(store, deployment)
    approved = revision.approval_id is not None
    newest = max(r.number for r in store.revisions(deployment.id))
    return s.ApprovalPreviewOut(
        deployment=deployment.id,
        revision=revision.number,
        release=found.release.name,
        authority_digest=revision.authority_digest,
        base_authority_digest=canonical.authority_digest(base),
        policy_revision=approvals.policy_revision(store, deployment),
        authority=revision.authority,
        base_authority=base,
        changes=widens(base, revision.authority),
        gate_failures=failures,
        observation=s.Observed(
            state=found.state,
            observed_at=found.observation.observed_at if found.observation else None,
            age_seconds=derive.age_seconds(found.observation, at),
        ),
        approved=approved,
        approvable=not approved and not failures and revision.number == newest,
    )


@router.post("/deployments/{deployment_id}/approvals", response_model=s.ApproveOut)
def approve(
    deployment_id: str,
    body: s.ApproveIn,
    store: StoreDep,
    settings: SettingsDep,
    who: Operator,
    at: Now,
) -> s.ApproveOut:
    """Approve a revision. Refused unless the binding matches and every gate passes."""
    deployment = _owned(store, deployment_id)
    if store.approval(body.approval_id) is not None:
        raise ApiError("replayed-approval", f"approval {body.approval_id} was already used")
    revision = _revision(store, deployment, body.revision)
    newest = max(r.number for r in store.revisions(deployment.id))
    if revision.number != newest:
        raise ApiError("stale-approval", f"revision {revision.number} is not the newest ({newest})")
    if revision.approval_id is not None:
        raise ApiError("stale-approval", f"revision {revision.number} already has an approval")
    if revision.phase not in ("awaiting-runtime", "pending-approval"):
        raise ApiError(
            "stale-approval", f"revision {revision.number} is {revision.phase}, not waiting"
        )
    if body.authority_digest != revision.authority_digest:
        raise ApiError("stale-approval", "the authority is not the one that was shown")
    policy = approvals.policy_revision(store, deployment)
    if body.policy_revision != policy:
        raise ApiError("stale-approval", "the tenant policy changed since it was shown")
    base = canonical.authority_digest(approvals.current_authority(store, deployment))
    if body.base_authority_digest != base:
        raise ApiError("stale-approval", "the current revision changed since it was shown")

    found = approvals.seen(
        store, deployment, revision, now=at, max_age_seconds=settings.observation_max_age_seconds
    )
    unreadable = approvals.problem_for(found, revision)
    if unreadable:
        raise ApiError("refused", unreadable)
    failures = approvals.gate_failures_of(store, deployment, found)
    if failures:
        raise ApiError("refused", "the release may not be approved: " + "; ".join(failures))

    with store.transaction(who.actor) as tx:
        seq = tx.event(
            "approved",
            deployment_id=deployment.id,
            detail={
                "approval": body.approval_id,
                "revision": revision.number,
                "authorityDigest": body.authority_digest,
                "baseAuthorityDigest": body.base_authority_digest,
                "policyRevision": body.policy_revision,
                "approvedBy": who.email,
            },
        ).seq
        tx.add_approval(
            Approval(
                id=body.approval_id,
                deployment_id=deployment.id,
                revision=revision.number,
                authority_digest=body.authority_digest,
                base_authority_digest=body.base_authority_digest,
                policy_revision=body.policy_revision,
                approved_by=who.email or "",
                approved_at=at,
                ticket=body.ticket,
                origin="registry",
                evidence=approvals.evidence(found),
            )
        )
        for other in approvals.registry_approvals(tx, deployment.id):
            if other.id != body.approval_id:
                tx.set_approval_state(other.id, "superseded")
        tx.set_revision_progress(
            deployment.id,
            revision.number,
            phase="awaiting-runtime",
            approval_id=body.approval_id,
            reason=APPROVED_WAITING,
            generation=seq,
        )
    return s.ApproveOut(
        approval=store.approval(body.approval_id),
        revision=store.revision(deployment.id, revision.number),
    )


@router.post("/deployments/{deployment_id}/enabled", response_model=s.DeploymentChangeOut)
def set_enabled(
    deployment_id: str, body: s.EnabledIn, store: StoreDep, who: Operator
) -> s.DeploymentChangeOut:
    deployment = _owned(store, deployment_id)
    if deployment.enabled == body.enabled:
        return s.DeploymentChangeOut(deployment=deployment, changed=False)
    with store.transaction(who.actor) as tx:
        seq = tx.event(
            "enabled" if body.enabled else "disabled",
            deployment_id=deployment.id,
            detail={"reason": body.reason},
        ).seq
        tx.put_deployment(
            deployment.model_copy(update={"enabled": body.enabled, "generation": seq})
        )
    return s.DeploymentChangeOut(deployment=store.deployment(deployment.id), changed=True)


@router.post("/deployments/{deployment_id}/pin", response_model=s.DeploymentChangeOut)
def pin(
    deployment_id: str,
    body: s.PinIn,
    store: StoreDep,
    settings: SettingsDep,
    who: Operator,
    at: Now,
) -> s.DeploymentChangeOut:
    """Hold the pointer on one activated revision, or let it go."""
    deployment = _owned(store, deployment_id)
    if body.revision is None:
        if deployment.pinned_revision is None:
            return s.DeploymentChangeOut(deployment=deployment, changed=False)
        activated = [r.number for r in store.revisions(deployment.id) if r.phase == "activated"]
        update = {"pinned_revision": None, "current_revision": max(activated, default=None)}
        event, detail = "unpinned", {"was": deployment.pinned_revision}
    else:
        revision = _revision(store, deployment, body.revision)
        if deployment.pinned_revision == revision.number == deployment.current_revision:
            return s.DeploymentChangeOut(deployment=deployment, changed=False)
        if revision.phase != "activated":
            raise ApiError("refused", f"revision {revision.number} has not been activated")
        found = approvals.seen(
            store,
            deployment,
            revision,
            now=at,
            max_age_seconds=settings.observation_max_age_seconds,
        )
        ready = found.observation and derive.condition(found.observation.document, "Ready")
        if found.state != "fresh" or ready.get("status") != "True":
            raise ApiError(
                "refused",
                f"revision {revision.number} is not Ready on a recent observation of the cluster",
            )
        update = {"pinned_revision": revision.number, "current_revision": revision.number}
        event, detail = "pinned", {"revision": revision.number}
    with store.transaction(who.actor) as tx:
        seq = tx.event(event, deployment_id=deployment.id, detail=detail).seq
        tx.put_deployment(deployment.model_copy(update=update | {"generation": seq}))
    return s.DeploymentChangeOut(deployment=store.deployment(deployment.id), changed=True)
