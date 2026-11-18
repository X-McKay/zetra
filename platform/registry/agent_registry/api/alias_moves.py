"""The assessment, alias move and alias check routes (`contract/dependencies.md` sections 5, 6
and 8). Starting, evaluating and cancelling are for a signed-in person with `registry-operator`;
reads are for a reader. Logic lives in `agent_registry/rules/assessment.py` and
`agent_registry/rules/alias_move.py`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends

from agent_registry import queries
from agent_registry.api import schemas as s
from agent_registry.api.auth import Principal, operator, reader
from agent_registry.api.deps import get_store, now, writable
from agent_registry.api.deps import get_settings
from agent_registry.config import Settings
from agent_registry.rules import alias_move, assessment
from agent_registry.store import Store

router = APIRouter(prefix="/v1", tags=["alias-moves"])

StoreDep = Annotated[Store, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
Operator = Annotated[Principal, Depends(operator)]
Now = Annotated[datetime, Depends(now)]

# The caller is checked before the mode, so that an unknown caller learns nothing.
WRITE = [Depends(operator), Depends(writable)]


@router.post("/assessments", response_model=s.AssessmentOut, dependencies=WRITE)
def create_assessment(
    body: s.AssessmentIn, store: StoreDep, who: Operator, at: Now
) -> s.AssessmentOut:
    """Compute and store what moving the alias to the candidate would touch."""
    with store.transaction(who.actor) as tx:
        made = assessment.create(
            tx,
            f"asm-{uuid.uuid4().hex[:16]}",
            body.cluster,
            body.alias,
            {"engine": body.candidate.engine, "release": body.candidate.release},
            actor=who.actor,
            now=at,
        )
    return queries.assessment_out(store, made)


@router.get(
    "/assessments/{assessment_id}", response_model=s.AssessmentOut, dependencies=[Depends(reader)]
)
def get_assessment(assessment_id: str, store: StoreDep) -> s.AssessmentOut:
    return queries.assessment_of(store, assessment_id)


@router.post("/alias-moves", response_model=s.AliasMoveOut, dependencies=WRITE)
def start_move(
    body: s.AliasMoveIn, store: StoreDep, settings: SettingsDep, who: Operator, at: Now
) -> s.AliasMoveOut:
    """A1 to A3: start the move an assessment describes."""
    seconds = body.evaluation_seconds or settings.alias_move_evaluation_seconds
    with store.transaction(who.actor) as tx:
        move = alias_move.a1_start(
            tx,
            body.assessment,
            f"mv-{uuid.uuid4().hex[:16]}",
            sessions=body.sessions,
            evaluation_seconds=seconds,
            actor=who.actor,
            now=at,
        )
    return queries.move_out(store, move)


@router.get("/alias-moves", response_model=s.AliasMovesOut, dependencies=[Depends(reader)])
def list_moves(
    store: StoreDep,
    cluster: str | None = None,
    alias: str | None = None,
    open: bool | None = None,
) -> s.AliasMovesOut:
    """Moves, newest first."""
    return queries.alias_moves(store, cluster, alias, open)


@router.get("/alias-moves/{move_id}", response_model=s.AliasMoveOut, dependencies=[Depends(reader)])
def get_move(move_id: str, store: StoreDep) -> s.AliasMoveOut:
    return queries.alias_move(store, move_id)


@router.post("/alias-moves/{move_id}/evaluate", response_model=s.AliasMoveOut, dependencies=WRITE)
def evaluate_move(
    move_id: str, store: StoreDep, settings: SettingsDep, who: Operator, at: Now
) -> s.AliasMoveOut:
    """A9: the engine is serving the candidate; evaluate the held agents on it."""
    with store.transaction(who.actor) as tx:
        alias_move.a9_evaluate(
            tx, move_id, now=at, max_age_seconds=settings.observation_max_age_seconds
        )
    return queries.alias_move(store, move_id)


@router.post("/alias-moves/{move_id}/cancel", response_model=s.AliasMoveOut, dependencies=WRITE)
def cancel_move(
    move_id: str, body: s.AliasMoveCancelIn, store: StoreDep, who: Operator, at: Now
) -> s.AliasMoveOut:
    """A5: cancel a move that is holding or held."""
    with store.transaction(who.actor) as tx:
        alias_move.a5_cancel(tx, move_id, body.reason, now=at)
    return queries.alias_move(store, move_id)


@router.post("/checks/alias", response_model=s.AliasCheckOut, dependencies=[Depends(reader)])
def check_alias(body: s.AliasCheckIn, store: StoreDep) -> s.AliasCheckOut:
    """Section 8: may a change that moves the alias to the candidate merge? Writes nothing."""
    found = alias_move.check(
        store,
        body.cluster,
        body.alias,
        {"engine": body.candidate.engine, "release": body.candidate.release},
    )
    return s.AliasCheckOut(**found)
