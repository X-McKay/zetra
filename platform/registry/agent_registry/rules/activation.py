"""Evaluations, and the moves that end a candidate's time as the newest revision
(`contract/delivery.md` sections 5 and 6). Each function takes a transaction and writes its
events itself; the caller settles the Agent in force with the sequence number it returns."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from agent_registry import canonical
from agent_registry.models import Deployment, Evaluation, Release, Revision, evaluation_id
from agent_registry.rules import Refusal, revisions, revocation
from agent_registry.rules.approval import owned
from agent_registry.store import Reads, Store, Tx

WAITING_FOR_EVALUATION = "waiting for evaluation"
MAX_ATTEMPTS = 3  # errors in one request before the revision is refused
FINAL = ("passed", "failed", "error")


# -- what a release asks for ---------------------------------------------------------------


def suite_of(release: Release) -> dict[str, Any] | None:
    """The suite the release carried, when it has cases."""
    suite = ((release.lineage or {}).get("evaluation") or {}).get("suite")
    if isinstance(suite, dict) and suite.get("cases"):
        return suite
    return None


def required(release: Release) -> bool:
    return suite_of(release) is not None


def attempts_of(reads: Reads, deployment_id: str, number: int) -> list[Evaluation]:
    """The evaluations of one revision, oldest first."""
    return [e for e in reads.evaluations(deployment_id) if e.revision == number]


def admission(reads: Reads, deployment_id: str, number: int | None) -> Evaluation | None:
    """The `passed` evaluation of a revision: what it was admitted by."""
    if number is None:
        return None
    return next((e for e in attempts_of(reads, deployment_id, number) if e.state == "passed"), None)


# -- the moves -----------------------------------------------------------------------------


def request(
    tx: Tx, deployment_id: str, revision: Revision, release: Release, *, now: datetime, **detail
) -> tuple[Evaluation, int]:
    """Create the revision's next attempt `pending` and set it `evaluating` (D8, D11, D14).
    Returns the attempt and the sequence number of the event."""
    suite = suite_of(release)
    assert suite is not None  # only a release that carries a suite is evaluated
    attempt = max((e.attempt for e in attempts_of(tx, deployment_id, revision.number)), default=0)
    evaluation = Evaluation(
        id=evaluation_id(deployment_id, revision.number, attempt + 1),
        deployment_id=deployment_id,
        revision=revision.number,
        attempt=attempt + 1,
        suite=suite,
        suite_digest=canonical.digest(suite),
        created_at=now,
    )
    seq = tx.event(
        "evaluation-requested",
        deployment_id=deployment_id,
        detail={
            "evaluation": evaluation.id,
            "revision": revision.number,
            "attempt": evaluation.attempt,
            **detail,
        },
    ).seq
    tx.add_evaluation(evaluation)
    tx.set_revision_progress(
        deployment_id, revision.number, phase="evaluating", reason=WAITING_FOR_EVALUATION
    )
    return evaluation, seq


def activate(
    tx: Tx, deployment_id: str, number: int, *, now: datetime, evaluation: Evaluation | None
) -> int:
    """D6, D9 and D15: the revision is activated, and the pointer moves to it unless the
    deployment is pinned. Returns the sequence number of the event."""
    deployment = tx.deployment(deployment_id)
    assert deployment is not None
    pinned = deployment.pinned_revision is not None
    detail: dict[str, Any] = {"revision": number, "pinned": pinned}
    if evaluation is not None:
        detail["evaluation"] = evaluation.id
    seq = tx.event("revision-activated", deployment_id=deployment_id, detail=detail).seq
    tx.set_revision_progress(deployment_id, number, phase="activated", activated_at=now, reason="")
    if not pinned:
        tx.put_deployment(
            deployment.model_copy(update={"current_revision": number, "generation": seq})
        )
    return seq


def refuse(tx: Tx, deployment_id: str, number: int, reason: str, *, now: datetime) -> int:
    """D7, D10 and D12: the revision is refused and its open evaluations are cancelled.
    The pointer does not move. Returns the sequence number of the event."""
    seq = tx.event(
        "revision-refused",
        deployment_id=deployment_id,
        detail={"revision": number, "reason": reason},
    ).seq
    tx.set_revision_progress(deployment_id, number, phase="refused", reason=reason)
    revisions.cancel_open_evaluations(tx, deployment_id, number, now=now)
    return seq


def _errors_in_this_request(attempts: list[Evaluation]) -> int:
    """How many attempts since the last request ended in `error`. A request ends in a refusal
    after exactly three errors, so the run of errors at the end, taken in threes, belongs to
    the current request (a `failed` or `passed` attempt ends the run)."""
    run = 0
    for found in reversed(attempts):
        if found.state != "error":
            break
        run += 1
    return run % MAX_ATTEMPTS or (MAX_ATTEMPTS if run else 0)


# -- claim ---------------------------------------------------------------------------------


def _claimable(reads: Reads, cluster: str, now: datetime) -> Evaluation | None:
    """The oldest evaluation of a cluster that is pending, or claimed with a lease that has run
    out, and whose revision is evaluating."""
    found = [
        *reads.evaluations(state="pending"),
        *(
            e
            for e in reads.evaluations(state="claimed")
            if e.claim_expires_at is None or e.claim_expires_at <= now
        ),
    ]
    for evaluation in sorted(found, key=lambda e: (e.created_at, e.id)):
        deployment = reads.deployment(evaluation.deployment_id)
        if deployment is None or deployment.cluster != cluster:
            continue
        revision = reads.revision(deployment.id, evaluation.revision)
        if revision is not None and revision.phase == "evaluating":
            return evaluation
    return None


def claim(
    store: Store, cluster: str, evaluator: str, lease_seconds: int, *, now: datetime
) -> tuple[Evaluation, Release, Deployment] | None:
    """Hand the oldest claimable evaluation of `cluster` to `evaluator` for `lease_seconds`.
    Returns it with its release and deployment, or None. Decides on plain reads first, then
    again in the transaction, so that two callers never get the same evaluation."""
    if store.meta().mode == "recovery" or _claimable(store, cluster, now) is None:
        return None
    with store.transaction(evaluator) as tx:
        if tx.meta().mode == "recovery":
            return None
        found = _claimable(tx, cluster, now)
        if found is None:
            return None
        expires = now + timedelta(seconds=lease_seconds)
        tx.event(
            "evaluation-claimed",
            deployment_id=found.deployment_id,
            detail={
                "evaluation": found.id,
                "evaluator": evaluator,
                "expiresAt": expires.isoformat(),
            },
        )
        claimed = tx.set_evaluation_progress(
            found.id, state="claimed", claimed_by=evaluator, claim_expires_at=expires
        )
        deployment = tx.deployment(found.deployment_id)
        revision = tx.revision(found.deployment_id, found.revision)
        release = tx.release(revision.release_id)
    return claimed, release, deployment


# -- result --------------------------------------------------------------------------------


def record_result(
    tx: Tx,
    deployment_id: str,
    number: int,
    attempt: int,
    *,
    evaluator: str,
    evaluator_version: str,
    verdict: str,
    summary: str,
    cases: list[dict[str, Any]],
    now: datetime,
) -> tuple[bool, str]:
    """Record an evaluator's verdict and move the revision by D9 to D12 (section 6.2).
    Returns whether anything was written and the revision's phase now."""
    meta = tx.meta()
    if meta.mode == "recovery":
        raise Refusal("in-recovery", f"the registry is in recovery ({meta.mode_reason})")
    found = tx.evaluation(evaluation_id(deployment_id, number, attempt))
    revision = tx.revision(deployment_id, number)
    if found is None or revision is None:
        raise Refusal("not-found", f"{deployment_id} has no evaluation {number}#{attempt}")
    deployment = owned(tx, deployment_id)

    if found.state == "cancelled":
        return False, revision.phase
    if found.state in FINAL:
        if found.claimed_by == evaluator and (found.result or {}).get("verdict") == verdict:
            return False, revision.phase
        raise Refusal("conflict", f"evaluation {found.id} already has the verdict {found.state}")
    if found.state != "claimed" or found.claimed_by != evaluator:
        holder = f"held by {found.claimed_by}" if found.claimed_by else "not claimed"
        raise Refusal("conflict", f"evaluation {found.id} is {holder}, not by {evaluator}")

    acceptance = (found.suite or {}).get("acceptance") or {}
    tx.event(
        "evaluation-recorded",
        deployment_id=deployment.id,
        detail={
            "evaluation": found.id,
            "evaluator": evaluator,
            "verdict": verdict,
            "summary": summary,
        },
    )
    tx.set_evaluation_progress(
        found.id,
        state=verdict,
        evaluator_version=evaluator_version,
        result={"verdict": verdict, "summary": summary, "acceptance": acceptance, "cases": cases},
        finished_at=now,
    )
    seq = None
    if revision.phase == "evaluating":
        seq = _move(tx, deployment.id, revision, found, verdict, summary, now=now)
    revisions.settle_agent(tx, deployment.id, seq)
    return True, tx.revision(deployment.id, number).phase


def _move(
    tx: Tx,
    deployment_id: str,
    revision: Revision,
    evaluation: Evaluation,
    verdict: str,
    summary: str,
    *,
    now: datetime,
) -> int:
    if verdict == "passed":
        return activate(tx, deployment_id, revision.number, now=now, evaluation=evaluation)
    if verdict == "failed":
        return refuse(tx, deployment_id, revision.number, f"evaluation failed: {summary}", now=now)
    # The attempt just recorded is `error` in the store already.
    if _errors_in_this_request(attempts_of(tx, deployment_id, revision.number)) < MAX_ATTEMPTS:
        release = tx.release(revision.release_id)
        assert release is not None
        _, seq = request(tx, deployment_id, revision, release, now=now, retry=True)
        return seq
    reason = f"evaluation could not be completed: {summary}"
    return refuse(tx, deployment_id, revision.number, reason, now=now)


# -- another evaluation --------------------------------------------------------------------


def request_again(
    tx: Tx, deployment_id: str, number: int, reason: str, *, now: datetime
) -> tuple[Evaluation, Revision]:
    """D14: an operator asks for another evaluation of the newest revision after the last one
    refused it."""
    deployment = owned(tx, deployment_id)
    revision = tx.revision(deployment.id, number)
    if revision is None:
        raise Refusal("not-found", f"{deployment.id} has no revision {number}")
    newest = max(r.number for r in tx.revisions(deployment.id))
    if revision.number != newest:
        raise Refusal("refused", f"revision {number} is not the newest ({newest})")
    attempts = attempts_of(tx, deployment.id, number)
    if revision.phase != "refused" or not attempts or attempts[-1].state not in ("failed", "error"):
        raise Refusal("refused", f"revision {number} was not refused by an evaluation")
    release = tx.release(revision.release_id)
    assert release is not None
    found = revocation.revoked(tx, release)
    if found is not None:
        raise Refusal("refused", f"release {release.name} is revoked ({found.id})")
    evaluation, seq = request(tx, deployment.id, revision, release, now=now, reason=reason)
    revisions.settle_agent(tx, deployment.id, seq)
    return evaluation, tx.revision(deployment.id, number)
