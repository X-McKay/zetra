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
from agent_registry.config import Settings
from agent_registry.rules import Refusal, lifecycle, report
from agent_registry.rules import approval as approvals
from agent_registry.rules.authority import widens
from agent_registry.store import Store

# The caller is checked before the mode, so that an unknown caller learns nothing.
router = APIRouter(
    prefix="/v1", tags=["write"], dependencies=[Depends(operator), Depends(writable)]
)

StoreDep = Annotated[Store, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
Operator = Annotated[Principal, Depends(operator)]
Now = Annotated[datetime, Depends(now)]


@router.post("/deployments/{deployment_id}/releases", response_model=s.RecordReleaseOut)
def record_release(
    deployment_id: str, body: s.RecordReleaseIn, store: StoreDep, who: Operator, at: Now
) -> s.RecordReleaseOut:
    """Record a published release and create the next revision, projected unapproved."""
    approvals.owned(store, deployment_id)  # in recovery, not found, the pull request path
    if body.evaluation is not None:
        report.check_suite(body.evaluation.suite)
    report.check_declaration(body.agent, body.agent_release)
    report.check_skills(body.agent_release)
    lineage = dict(body.lineage)
    if body.evaluation is not None:
        lineage["evaluation"] = {"suite": body.evaluation.suite}
    with store.transaction(who.actor) as tx:
        release_id, number, created = lifecycle.record_release(
            tx, deployment_id, body.agent, body.agent_release, lineage, now=at
        )
    return s.RecordReleaseOut(
        release=store.release(release_id),
        revision=store.revision(deployment_id, number),
        created=created,
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
    deployment = approvals.owned(store, deployment_id)
    revision = store.revision(deployment.id, number)
    if revision is None:
        raise Refusal("not-found", f"{deployment.id} has no revision {number}")
    found = approvals.seen(
        store, deployment, revision, now=at, max_age_seconds=settings.observation_max_age_seconds
    )
    unreadable = approvals.problem_for(found, revision)
    failures = (
        [unreadable]
        if unreadable
        else approvals.gate_failures_of(store, deployment, found, revision)
    )
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
    with store.transaction(who.actor) as tx:
        approvals.approve(
            tx,
            deployment_id,
            approval_id=body.approval_id,
            revision=body.revision,
            authority_digest=body.authority_digest,
            base_authority_digest=body.base_authority_digest,
            policy_revision_seen=body.policy_revision,
            ticket=body.ticket,
            approver=who.email or "",
            now=at,
            max_age_seconds=settings.observation_max_age_seconds,
        )
    return s.ApproveOut(
        approval=store.approval(body.approval_id),
        revision=store.revision(deployment_id, body.revision),
    )


@router.post("/deployments/{deployment_id}/enabled", response_model=s.DeploymentChangeOut)
def set_enabled(
    deployment_id: str, body: s.EnabledIn, store: StoreDep, who: Operator
) -> s.DeploymentChangeOut:
    with store.transaction(who.actor) as tx:
        changed = lifecycle.set_enabled(tx, deployment_id, body.enabled, body.reason)
    return s.DeploymentChangeOut(deployment=store.deployment(deployment_id), changed=changed)


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
    with store.transaction(who.actor) as tx:
        changed = lifecycle.pin(
            tx,
            deployment_id,
            body.revision,
            now=at,
            max_age_seconds=settings.observation_max_age_seconds,
        )
    return s.DeploymentChangeOut(deployment=store.deployment(deployment_id), changed=changed)


@router.post("/deployments/{deployment_id}/rollback", response_model=s.DeploymentChangeOut)
def rollback(
    deployment_id: str,
    body: s.RollbackIn,
    store: StoreDep,
    settings: SettingsDep,
    who: Operator,
    at: Now,
) -> s.DeploymentChangeOut:
    """Return to an earlier release: the given revision, or the newest earlier activated one."""
    with store.transaction(who.actor) as tx:
        changed = lifecycle.rollback(
            tx,
            deployment_id,
            body.revision,
            body.reason,
            now=at,
            max_age_seconds=settings.observation_max_age_seconds,
        )
    return s.DeploymentChangeOut(deployment=store.deployment(deployment_id), changed=changed)
