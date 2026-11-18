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
from agent_registry.rules import assessment
from agent_registry.store import Store

router = APIRouter(prefix="/v1", tags=["alias-moves"])

StoreDep = Annotated[Store, Depends(get_store)]
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
