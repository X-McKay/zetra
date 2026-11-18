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
from agent_registry.store import Store

router = APIRouter(prefix="/v1", tags=["authority"])

StoreDep = Annotated[Store, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
Operator = Annotated[Principal, Depends(operator)]
Now = Annotated[datetime, Depends(now)]


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
    raise NotImplementedError


@router.post(
    "/deployments/{deployment_id}/authority/return",
    response_model=s.ReturnOut,
    dependencies=[Depends(operator), Depends(writable)],
)
def give_back(deployment_id: str, store: StoreDep, who: Operator) -> s.ReturnOut:
    """Move an agent back to the pull request path and return the documents to commit."""
    raise NotImplementedError


@router.get("/recovery", response_model=s.RecoveryOut, dependencies=[Depends(reader)])
def recovery(store: StoreDep, settings: SettingsDep, at: Now) -> s.RecoveryOut:
    """The mode and, in recovery, every difference between the registry and the clusters."""
    raise NotImplementedError


@router.post("/recovery/leave", response_model=s.LeaveRecoveryOut)
def leave_recovery(
    body: s.LeaveRecoveryIn, store: StoreDep, settings: SettingsDep, who: Operator, at: Now
) -> s.LeaveRecoveryOut:
    """Adopt what the clusters hold and return to normal. `conflict` when the digest named is
    not the digest of the differences now, or the report is not complete."""
    raise NotImplementedError
