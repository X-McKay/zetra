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

from agent_registry.api import schemas as s
from agent_registry.api.auth import Principal, operator
from agent_registry.api.deps import get_settings, get_store, now, writable
from agent_registry.config import Settings
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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


@router.post("/deployments/{deployment_id}/enabled", response_model=s.DeploymentChangeOut)
def set_enabled(
    deployment_id: str, body: s.EnabledIn, store: StoreDep, who: Operator
) -> s.DeploymentChangeOut:
    raise NotImplementedError


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
    raise NotImplementedError
