"""The evaluation routes (`contract/delivery.md` section 6.2). Callers: the evaluator of the
cluster (claim and result) and a signed-in operator (another evaluation). Logic lives in
`agent_registry/rules/activation.py`."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from agent_registry.api import schemas as s
from agent_registry.api.auth import (
    Principal,
    evaluator,
    evaluator_cluster,
    evaluator_of_any_cluster,
    operator,
)
from agent_registry.api.deps import get_store, now, writable
from agent_registry.api.errors import ApiError
from agent_registry.rules import Refusal, activation
from agent_registry.store import Store

router = APIRouter(prefix="/v1", tags=["evaluations"])

StoreDep = Annotated[Store, Depends(get_store)]
Evaluator = Annotated[Principal, Depends(evaluator)]
AnyEvaluator = Annotated[Principal, Depends(evaluator_of_any_cluster)]
Operator = Annotated[Principal, Depends(operator)]
Now = Annotated[datetime, Depends(now)]


@router.post("/clusters/{cluster}/evaluations/claim", response_model=s.ClaimOut)
def claim(cluster: str, body: s.ClaimIn, store: StoreDep, who: Evaluator, at: Now) -> s.ClaimOut:
    """The oldest evaluation of the cluster that is waiting for an evaluator, or null. Null
    in recovery too."""
    if store.cluster(cluster) is None:
        raise Refusal("not-found", f"there is no cluster {cluster}")
    found = activation.claim(store, cluster, body.evaluator, body.lease_seconds, now=at)
    if found is None:
        return s.ClaimOut(evaluation=None)
    evaluation, release, deployment = found
    assert evaluation.claim_expires_at is not None
    return s.ClaimOut(
        evaluation=s.ClaimedEvaluation(
            deployment=deployment.id,
            revision=evaluation.revision,
            attempt=evaluation.attempt,
            agent=deployment.item,
            tenant=deployment.tenant or "",
            namespace=deployment.namespace,
            release=release.name,
            suite=evaluation.suite,
            suite_digest=evaluation.suite_digest,
            claim_expires_at=evaluation.claim_expires_at,
        )
    )


@router.post(
    "/deployments/{deployment_id}/revisions/{number}/evaluations/{attempt}/result",
    response_model=s.ResultOut,
)
def result(
    deployment_id: str,
    number: int,
    attempt: int,
    body: s.ResultIn,
    store: StoreDep,
    who: AnyEvaluator,
    at: Now,
) -> s.ResultOut:
    """An evaluator's verdict. The revision moves by D9 to D12 in the same transaction."""
    deployment = store.deployment(deployment_id)
    if deployment is None or deployment.kind != "agent":
        raise Refusal("not-found", f"there is no agent deployment {deployment_id}")
    if evaluator_cluster(who) != deployment.cluster:
        raise ApiError.forbidden(f"this token is not the evaluator of cluster {deployment.cluster}")
    with store.transaction(body.evaluator) as tx:
        recorded, phase = activation.record_result(
            tx,
            deployment_id,
            number,
            attempt,
            evaluator=body.evaluator,
            evaluator_version=body.evaluator_version,
            verdict=body.verdict,
            summary=body.summary,
            cases=[c.model_dump(by_alias=True) for c in body.cases],
            now=at,
        )
    return s.ResultOut(recorded=recorded, phase=phase)


@router.post(
    "/deployments/{deployment_id}/revisions/{number}/evaluations",
    response_model=s.EvaluationRequestOut,
    dependencies=[Depends(writable)],
)
def request_again(
    deployment_id: str,
    number: int,
    body: s.EvaluationRequestIn,
    store: StoreDep,
    who: Operator,
    at: Now,
) -> s.EvaluationRequestOut:
    """D14: another evaluation of a revision the last one refused."""
    with store.transaction(who.actor) as tx:
        evaluation, revision = activation.request_again(
            tx, deployment_id, number, body.reason, now=at
        )
    return s.EvaluationRequestOut(evaluation=evaluation, revision=revision)
