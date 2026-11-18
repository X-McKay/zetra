"""The cluster routes (package 1.4b). Caller: the provisioner of the named cluster only.

Rules: `contract/projection.md`. Logic lives in `agent_registry/rules/projection.py` and
`agent_registry/rules/intake.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from agent_registry.api import schemas as s
from agent_registry.api.auth import Principal, provisioner
from agent_registry.api.deps import get_settings, get_store, now
from agent_registry.config import Settings
from agent_registry.store import Store

router = APIRouter(prefix="/v1/clusters/{cluster}", tags=["cluster"])

StoreDep = Annotated[Store, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
Provisioner = Annotated[Principal, Depends(provisioner)]
Now = Annotated[datetime, Depends(now)]


@router.get("/desired", response_model=s.DesiredOut)
def desired(
    cluster: str,
    store: StoreDep,
    who: Provisioner,
    last_generation: Annotated[int, Query(alias="lastGeneration", ge=0)] = 0,
) -> s.DesiredOut:
    """Everything the provisioner should apply and observe. If `lastGeneration` is higher
    than the registry's generation the registry has gone back in time: it enters recovery
    and answers with no objects."""
    raise NotImplementedError


@router.post("/observations", response_model=s.ObservationsOut)
def observations(
    cluster: str,
    body: s.ObservationsIn,
    store: StoreDep,
    settings: SettingsDep,
    who: Provisioner,
    at: Now,
) -> s.ObservationsOut:
    """Store what the provisioner saw and did, then let each registry-owned agent of the
    cluster advance. Accepted in recovery too: leaving recovery needs fresh observations."""
    raise NotImplementedError
