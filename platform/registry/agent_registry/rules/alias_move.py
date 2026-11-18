"""Moving an alias while its engine is swapped (`contract/dependencies.md` section 6). One
function per transition A1 to A13, each a function of a transaction that writes its events
itself. `advance` applies A12 and A13 on every provisioner pass."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from agent_registry import derive
from agent_registry.models import (
    IN_FORCE,
    OPEN_MOVE,
    AliasMove,
    Deployment,
    Hold,
    Revision,
    deployment_id,
    release_id,
)
from agent_registry.rules import Refusal, assessment
from agent_registry.store import Reads, Store, Tx

WAITING_MOVES = (
    "holding",
    "held",
    "moving",
)  # while these, D6 and D8 do not happen for a held agent
EXPIRED = "evaluation did not finish in the time allowed"
MOVED_ELSEWHERE = "the alias moved to another candidate"
MIN_EVALUATION_SECONDS, MAX_EVALUATION_SECONDS = 60, 86400


def held_reason(move_id: str) -> str:
    return f"held by alias move {move_id}"


# -- reads ---------------------------------------------------------------------------------


def hold_in_force(reads: Reads, deployment: str) -> Hold | None:
    """The one hold of a deployment that is not resumed or released."""
    return next((h for h in reads.holds(deployment_id=deployment) if h.state in IN_FORCE), None)


def waiting_for(reads: Reads, deployment: str) -> str | None:
    """The id of the move a deployment's revision must wait for (D6 and D8 do not happen
    while it does), or None."""
    hold = hold_in_force(reads, deployment)
    if hold is None:
        return None
    move = reads.alias_move(hold.move_id)
    return move.id if move is not None and move.state in WAITING_MOVES else None


def open_move(reads: Reads, cluster: str, alias: str) -> AliasMove | None:
    return next(iter(reads.alias_moves(cluster=cluster, alias=alias, open=True)), None)


def in_open_move(reads: Reads, deployment: Deployment) -> AliasMove | None:
    """An open move of an alias the deployment's agent depends on, if there is one."""
    revisions = assessment.looked_at(reads, deployment)
    for move in reads.alias_moves(cluster=deployment.cluster, open=True):
        if assessment.uses_alias(reads, revisions, move.alias):
            return move
    return None


def _raise_generation(tx: Tx, deployment: str, seq: int) -> None:
    """Have the Agent of a deployment written again."""
    found = tx.deployment(deployment)
    if found is not None and found.generation < seq:
        tx.put_deployment(found.model_copy(update={"generation": seq}))


# -- A1 to A3 ------------------------------------------------------------------------------


def a1_start(
    tx: Tx,
    assessment_id: str,
    move_id: str,
    *,
    sessions: str,
    evaluation_seconds: int,
    actor: str,
    now: datetime,
) -> AliasMove:
    """A1: start a move for an assessment. A2: refused `stale-assessment` when it no longer
    describes the cluster (decided first). A3: refused `refused` when it is blocked and
    `conflict` when the alias has an open move."""
    found = tx.assessment(assessment_id)
    if found is None:
        raise Refusal("not-found", f"there is no assessment {assessment_id}")
    changed = assessment.staleness(tx, found)
    if changed:
        raise Refusal("stale-assessment", "the assessment is stale: " + "; ".join(changed))
    if found.blocked:
        raise Refusal(
            "refused", "the assessment is blocked: the candidate does not offer what an agent needs"
        )
    existing = open_move(tx, found.cluster, found.alias)
    if existing is not None:
        raise Refusal("conflict", f"alias {found.alias} already has an open move ({existing.id})")

    held = [d for d in found.dependants if d["held"]]
    released = []
    for dependant in held:
        old = hold_in_force(tx, dependant["deployment"])
        if old is None:
            continue
        other = tx.alias_move(old.move_id)
        if other is not None and other.state in OPEN_MOVE:
            raise Refusal(
                "conflict", f"{dependant['agent']} is already held by alias move {old.move_id}"
            )
        released.append((old, dependant["deployment"]))
    seq = tx.event(
        "alias-move-started",
        detail={
            "move": move_id,
            "assessment": found.id,
            "cluster": found.cluster,
            "alias": found.alias,
            "candidate": found.candidate,
            "sessions": sessions,
            "holds": [d["agent"] for d in held],
        },
    ).seq
    move = AliasMove(
        id=move_id,
        assessment_id=found.id,
        cluster=found.cluster,
        alias=found.alias,
        candidate=found.candidate,
        sessions=sessions,
        evaluation_seconds=evaluation_seconds,
        started_by=actor,
        started_at=now,
        state="holding" if held else "held",
        held_at=None if held else now,
    )
    tx.add_alias_move(move)
    for old, _ in released:
        # A suspended hold of an earlier move gives way to this one.
        tx.set_hold_progress(
            old.move_id,
            old.deployment_id,
            state="released",
            ended_at=now,
            reason=f"superseded by alias move {move_id}",
        )
    for dependant in held:
        tx.add_hold(Hold(move_id=move_id, deployment_id=dependant["deployment"]))
        _raise_generation(tx, dependant["deployment"], seq)
    return move


# -- A4 ------------------------------------------------------------------------------------


def a4_acknowledge(tx: Tx, move_id: str, deployment: str, running: int, *, now: datetime) -> bool:
    """A4: an observation report counts the running task sessions of a held agent. With none
    the hold becomes `held`, and the move with it when every hold is. An entry for an unknown
    or finished hold is ignored. Returns whether anything was written."""
    hold = next(iter(tx.holds(move_id=move_id, deployment_id=deployment)), None)
    move = tx.alias_move(move_id)
    if hold is None or move is None or hold.state not in IN_FORCE:
        return False
    confirmed = running == 0 and hold.state == "holding"
    if not confirmed and hold.running == running:
        return False
    tx.event(
        "hold-held" if confirmed else "hold-counted",
        deployment_id=deployment,
        detail={"move": move_id, "running": running},
    )
    if confirmed:
        tx.set_hold_progress(move_id, deployment, state="held", running=0, acknowledged_at=now)
    else:
        tx.set_hold_progress(move_id, deployment, running=running)
    if confirmed and move.state == "holding":
        if all(h.state == "held" for h in tx.holds(move_id=move_id)):
            tx.event("alias-move-held", detail={"move": move_id, "alias": move.alias})
            tx.set_alias_move_progress(move_id, state="held", held_at=now)
    return True


# -- A5 ------------------------------------------------------------------------------------


def _cancel(tx: Tx, move: AliasMove, reason: str, *, now: datetime) -> int:
    seq = tx.event(
        "alias-move-cancelled",
        detail={"move": move.id, "alias": move.alias, "reason": reason},
    ).seq
    tx.set_alias_move_progress(move.id, state="cancelled", finished_at=now, reason=reason)
    for hold in tx.holds(move_id=move.id):
        if hold.state in IN_FORCE:
            tx.set_hold_progress(
                move.id, hold.deployment_id, state="released", ended_at=now, reason=reason
            )
            _raise_generation(tx, hold.deployment_id, seq)
    return seq


def a5_cancel(tx: Tx, move_id: str, reason: str, *, now: datetime) -> AliasMove:
    """A5: cancel a move that is `holding` or `held`; its holds are released. Any other state
    is refused."""
    move = tx.alias_move(move_id)
    if move is None:
        raise Refusal("not-found", f"there is no alias move {move_id}")
    if move.state not in ("holding", "held"):
        raise Refusal(
            "refused",
            f"alias move {move_id} is {move.state}; only a holding or held move is cancelled",
        )
    _cancel(tx, move, reason or "cancelled by an operator", now=now)
    return tx.alias_move(move_id)


# -- A6 to A8: the feed --------------------------------------------------------------------


def a6_moved(tx: Tx, move: AliasMove, was: dict[str, str], *, now: datetime) -> None:
    """A6: the feed moved the alias to the candidate of a `held` move."""
    tx.event(
        "alias-moved",
        detail={"move": move.id, "alias": move.alias, "from": was, "to": move.candidate},
    )
    tx.set_alias_move_progress(move.id, state="moving", moved_at=now)


def a7_moved_before_hold_confirmed(
    tx: Tx, move: AliasMove, was: dict[str, str], *, now: datetime
) -> None:
    """A7: the same, while the move is still `holding`. The registry follows the cluster."""
    a6_moved(tx, move, was, now=now)
    tx.event(
        "alias-moved-before-hold-confirmed",
        detail={"move": move.id, "alias": move.alias, "unconfirmed": _unconfirmed(tx, move)},
    )


def a8_changed_without_move(
    tx: Tx,
    cluster: str,
    alias: str,
    was: dict[str, str],
    now_pair: dict[str, str],
    *,
    now: datetime,
) -> None:
    """A8: the feed moved the alias to something that is not the candidate of an open move.
    An open move that is `holding` or `held` is cancelled."""
    tx.event(
        "alias-changed-without-move",
        detail={"cluster": cluster, "alias": alias, "from": was, "to": now_pair},
    )
    move = open_move(tx, cluster, alias)
    if move is not None and move.state in ("holding", "held"):
        _cancel(tx, move, MOVED_ELSEWHERE, now=now)


def _unconfirmed(reads: Reads, move: AliasMove) -> list[str]:
    return [h.deployment_id for h in reads.holds(move_id=move.id) if h.state == "holding"]


def note_feed(tx: Tx, before: dict[tuple[str, str], dict[str, str]], *, now: datetime) -> None:
    """Called from `apply_feed` once the aliases and engines are written and before the
    dependants' new revisions are recorded: `before` is `assessment.pairs` as it was."""
    after = assessment.pairs(tx)
    for (cluster, alias), was in sorted(before.items()):
        is_now = after.get((cluster, alias))
        if is_now is None or is_now == was:
            continue
        move = open_move(tx, cluster, alias)
        if move is not None and move.candidate == is_now:
            if move.state == "held":
                a6_moved(tx, move, was, now=now)
            elif move.state == "holding":
                a7_moved_before_hold_confirmed(tx, move, was, now=now)
        else:
            a8_changed_without_move(tx, cluster, alias, was, is_now, now=now)


# -- A9 ------------------------------------------------------------------------------------


def a9_evaluate(tx: Tx, move_id: str, *, now: datetime, max_age_seconds: int) -> AliasMove:
    """A9: an operator says the engine is serving. Refused unless the move is `moving` and
    the newest observation of the candidate's engine is fresh, ready and shows the
    candidate's served name."""
    move = tx.alias_move(move_id)
    if move is None:
        raise Refusal("not-found", f"there is no alias move {move_id}")
    if move.state != "moving":
        raise Refusal("refused", f"alias move {move_id} is {move.state}, not moving")
    engine = tx.deployment(deployment_id("model", move.cluster, None, move.candidate["engine"]))
    manifest = tx.release(release_id("model", move.candidate["engine"], move.candidate["release"]))
    wanted = (manifest.document.get("servedName") if manifest else None) or ""
    missing = []
    if engine is None:
        missing.append(f"engine {move.candidate['engine']} is not stored")
    else:
        namespace = engine.document.get("namespace") or engine.namespace
        seen = tx.observation(move.cluster, "model-engine", namespace, engine.item)
        state = derive.freshness(seen, now, max_age_seconds)
        if seen is None or state != "fresh":
            missing.append(f"no fresh observation of engine {engine.item} ({state})")
        else:
            ready = seen.document.get("readyReplicas")
            if not isinstance(ready, int) or ready < 1:
                missing.append(f"engine {engine.item} has no ready replica")
            if seen.document.get("servedName") != wanted:
                missing.append(
                    f"engine {engine.item} serves {seen.document.get('servedName')!r}, not {wanted!r}"
                )
    if missing:
        raise Refusal("refused", "the engine is not serving the candidate: " + "; ".join(missing))
    deadline = now + timedelta(seconds=move.evaluation_seconds)
    tx.event(
        "alias-move-evaluating",
        detail={"move": move.id, "alias": move.alias, "deadline": deadline.isoformat()},
    )
    return tx.set_alias_move_progress(
        move.id, state="evaluating", evaluating_at=now, deadline=deadline
    )


# -- A10 and A11: where a revision is activated or refused ---------------------------------


def a10_resume(tx: Tx, deployment: str, *, now: datetime) -> int | None:
    """A10: a revision of a deployment whose hold is in force is activated while the move is
    `evaluating` or `completed`. The hold is resumed and the Agent written again. Returns the
    event's sequence number, or None when nothing applies."""
    hold = hold_in_force(tx, deployment)
    move = tx.alias_move(hold.move_id) if hold else None
    if hold is None or move is None or move.state not in ("evaluating", "completed"):
        return None
    seq = tx.event(
        "hold-resumed", deployment_id=deployment, detail={"move": move.id, "alias": move.alias}
    ).seq
    tx.set_hold_progress(move.id, deployment, state="resumed", ended_at=now)
    _raise_generation(tx, deployment, seq)
    return seq


def a11_suspend(
    tx: Tx, deployment: str, revision: Revision | None, reason: str, *, now: datetime
) -> None:
    """A11: the newest revision of a deployment whose hold is `held` is refused while the
    move is `evaluating`. The hold is suspended with the revision's reason."""
    hold = hold_in_force(tx, deployment)
    move = tx.alias_move(hold.move_id) if hold else None
    if hold is None or move is None or hold.state != "held" or move.state != "evaluating":
        return
    newest = tx.revisions(deployment)[-1:]
    if revision is None or not newest or newest[0].number != revision.number:
        return
    tx.event(
        "hold-suspended",
        deployment_id=deployment,
        detail={"move": move.id, "alias": move.alias, "reason": reason},
    )
    tx.set_hold_progress(move.id, deployment, state="suspended", reason=reason)


# -- A12 and A13 ---------------------------------------------------------------------------


def _decide(reads: Reads, move: AliasMove, now: datetime) -> bool:
    """Whether an evaluating move has something to do: A12 or A13."""
    held = [h for h in reads.holds(move_id=move.id) if h.state == "held"]
    return not held or (move.deadline is not None and now >= move.deadline)


def a12_expire(tx: Tx, move: AliasMove, *, now: datetime) -> int:
    """A12: the deadline has passed. Every hold still `held` is suspended, one event each.
    An evaluation still running is left to finish. Returns how many holds changed."""
    count = 0
    if move.deadline is not None and now >= move.deadline:
        for hold in tx.holds(move_id=move.id):
            if hold.state == "held":
                tx.event(
                    "hold-suspended",
                    deployment_id=hold.deployment_id,
                    detail={"move": move.id, "alias": move.alias, "reason": EXPIRED},
                )
                tx.set_hold_progress(move.id, hold.deployment_id, state="suspended", reason=EXPIRED)
                count += 1
    return count


def a13_complete(tx: Tx, move: AliasMove, *, now: datetime) -> bool:
    """A13: no hold is `held`; the move is completed. Returns whether it was."""
    if any(h.state == "held" for h in tx.holds(move_id=move.id)):
        return False
    tx.event("alias-move-completed", detail={"move": move.id, "alias": move.alias})
    tx.set_alias_move_progress(move.id, state="completed", finished_at=now)
    return True


def advance(store: Store, cluster: str, *, now: datetime, actor: str = "registry") -> list[str]:
    """A12 and A13 for every evaluating move of a cluster. Returns the ids of the moves that
    changed. Decides on plain reads first, then again in the transaction."""
    changed = []
    for move in store.alias_moves(cluster=cluster, open=True):
        if move.state != "evaluating" or not _decide(store, move, now):
            continue
        with store.transaction(actor) as tx:
            current = tx.alias_move(move.id)
            if current is None or current.state != "evaluating" or not _decide(tx, current, now):
                continue
            a12_expire(tx, current, now=now)
            a13_complete(tx, current, now=now)
        changed.append(move.id)
    return changed


# -- enabling, and the check a pull request asks for ---------------------------------------


def refuse_enable(reads: Reads, deployment: Deployment) -> None:
    """Section 6.2, "Enabling": an agent of an alias that has an open move cannot be enabled,
    for it would take sessions with no hold."""
    move = in_open_move(reads, deployment)
    if move is not None:
        raise Refusal(
            "refused",
            f"{deployment.item} depends on alias {move.alias}, which has an open move ({move.id})",
        )


def check(reads: Reads, cluster: str, alias: str, candidate: dict[str, str]) -> dict[str, Any]:
    """Section 8: is a change that moves the alias to the candidate allowed to merge? Writes
    nothing. Allowed only with a `held` move for this pair whose assessment is not stale."""
    if reads.cluster(cluster) is None:
        raise Refusal("not-found", f"there is no cluster {cluster}")
    if assessment.find_alias(reads, cluster, alias) is None:
        raise Refusal("not-found", f"there is no alias {alias} on cluster {cluster}")
    moves = [m for m in reads.alias_moves(cluster=cluster, alias=alias) if m.candidate == candidate]
    name = f"{candidate['engine']}/{candidate['release']}"

    def answer(move: AliasMove | None, reason: str) -> dict[str, Any]:
        return {
            "allowed": not reason,
            "move": move.id if move else None,
            "assessment": move.assessment_id if move else None,
            "reason": reason,
        }

    held = next((m for m in moves if m.state == "held"), None)
    if held is not None:
        stored = reads.assessment(held.assessment_id)
        changed = assessment.staleness(reads, stored) if stored else ["its assessment is missing"]
        if changed:
            return answer(held, f"the assessment of move {held.id} is stale: " + "; ".join(changed))
        return answer(held, "")
    found = next((m for m in moves if m.state in ("holding", "moving", "evaluating")), None)
    if found is None:
        return answer(None, f"there is no move for {name}")
    if found.state == "holding":
        agents = ", ".join(sorted(_agent(reads, d) for d in _unconfirmed(reads, found)))
        return answer(
            found, f"move {found.id} is still holding: waiting for the agents to stop: {agents}"
        )
    return answer(found, f"move {found.id} is {found.state}, not held")


def _agent(reads: Reads, deployment: str) -> str:
    found = reads.deployment(deployment)
    return found.item if found else deployment
