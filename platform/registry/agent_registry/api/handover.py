"""Authority and recovery routes (package 1.4b).

Rules: `contract/projection.md`, sections "Authority" and "Recovery". Logic lives in
`agent_registry/rules/handover.py` and `agent_registry/rules/recovery.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from agent_registry.api import schemas as s
from agent_registry.api.auth import Principal, operator, reader
from agent_registry.api.deps import get_settings, get_store, now, writable
from agent_registry.config import Settings
from agent_registry.rules import handover as authority
from agent_registry.rules import recovery as recoveries
from agent_registry.store import Store

router = APIRouter(prefix="/v1", tags=["authority"])

StoreDep = Annotated[Store, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
Operator = Annotated[Principal, Depends(operator)]
Now = Annotated[datetime, Depends(now)]


def _difference(found: recoveries.Difference) -> s.Difference:
    return s.Difference(
        deployment=found.deployment,
        subject=found.subject,
        field=found.field,
        registry=found.registry,
        cluster=found.cluster,
    )


@router.post(
    "/deployments/{deployment_id}/authority/take",
    response_model=s.DeploymentChangeOut,
    dependencies=[Depends(operator), Depends(writable)],
)
def take(
    deployment_id: str, store: StoreDep, settings: SettingsDep, who: Operator, at: Now
) -> s.DeploymentChangeOut:
    """Move an agent from the pull request path to the registry. `conflict` unless the
    registry's records equal fresh observations of the live objects."""
    with store.transaction(who.actor) as tx:
        changed = authority.take(
            tx, deployment_id, now=at, max_age_seconds=settings.observation_max_age_seconds
        )
    return s.DeploymentChangeOut(deployment=store.deployment(deployment_id), changed=changed)


@router.post(
    "/deployments/{deployment_id}/authority/return",
    response_model=s.ReturnOut,
    dependencies=[Depends(operator), Depends(writable)],
)
def give_back(deployment_id: str, store: StoreDep, who: Operator) -> s.ReturnOut:
    """Move an agent back to the pull request path and return the documents to commit."""
    with store.transaction(who.actor) as tx:
        changed = authority.give_back(tx, deployment_id)
    deployment = store.deployment(deployment_id)
    agent, releases = authority.documents_to_commit(store, deployment)
    return s.ReturnOut(deployment=deployment, changed=changed, agent=agent, agent_releases=releases)


@router.get("/recovery", response_model=s.RecoveryOut, dependencies=[Depends(reader)])
def recovery(store: StoreDep, settings: SettingsDep, at: Now) -> s.RecoveryOut:
    """The mode and, in recovery, every difference between the registry and the clusters."""
    found = recoveries.report(store, now=at, max_age_seconds=settings.observation_max_age_seconds)
    return s.RecoveryOut(
        mode=found.mode,
        reason=found.reason,
        complete=found.complete,
        waiting_for=found.waiting_for,
        differences=[_difference(d) for d in found.differences],
        digest=found.digest,
    )


@router.post("/recovery/leave", response_model=s.LeaveRecoveryOut)
def leave_recovery(
    body: s.LeaveRecoveryIn, store: StoreDep, settings: SettingsDep, who: Operator, at: Now
) -> s.LeaveRecoveryOut:
    """Adopt what the clusters hold and return to normal. `conflict` when the digest named is
    not the digest of the differences now, or the report is not complete."""
    with store.transaction(who.actor) as tx:
        generation, adopted = recoveries.leave(
            tx,
            body.digest,
            now=at,
            max_age_seconds=settings.observation_max_age_seconds,
        )
    return s.LeaveRecoveryOut(
        mode=store.meta().mode,
        generation=generation,
        adopted=[_difference(d) for d in adopted],
    )
