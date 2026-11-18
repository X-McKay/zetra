"""The read routes (package 1.3). Caller: `registry-reader` or `registry-operator`.

Paths, parameters and response models are the contract. Handlers are built by package 1.3;
the logic lives in `agent_registry/queries.py` so that it can be tested without HTTP.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from agent_registry import queries
from agent_registry.api import schemas as s
from agent_registry.api.auth import reader
from agent_registry.api.deps import get_settings, get_store, now
from agent_registry.config import Settings
from agent_registry.models import Authority, Kind
from agent_registry.store import Store

router = APIRouter(prefix="/v1", tags=["read"], dependencies=[Depends(reader)])

StoreDep = Annotated[Store, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
Now = Annotated[datetime, Depends(now)]


@router.get("/meta", response_model=s.MetaOut)
def meta(store: StoreDep) -> s.MetaOut:
    """The mode and the generation."""
    return queries.meta(store)


@router.get("/items", response_model=s.ItemsOut)
def items(store: StoreDep, kind: Kind | None = None) -> s.ItemsOut:
    return queries.items(store, kind)


@router.get("/items/{kind}/{name}", response_model=s.ItemOut)
def item(store: StoreDep, kind: Kind, name: str) -> s.ItemOut:
    """One item with its releases and its deployments. `not-found` when there is none."""
    return queries.item(store, kind, name)


@router.get("/releases/{release_id}", response_model=s.ReleaseOut)
def release(store: StoreDep, release_id: str) -> s.ReleaseOut:
    return queries.release(store, release_id)


@router.get("/deployments", response_model=s.DeploymentsOut)
def deployments(
    store: StoreDep,
    kind: Kind | None = None,
    cluster: str | None = None,
    tenant: str | None = None,
    authority: Authority | None = None,
) -> s.DeploymentsOut:
    return queries.deployments(store, kind, cluster, tenant, authority)


@router.get("/deployments/{deployment_id}", response_model=s.DeploymentOut)
def deployment(store: StoreDep, deployment_id: str) -> s.DeploymentOut:
    """A deployment with its revisions (ascending) and its approvals."""
    return queries.deployment(store, deployment_id)


@router.get("/deployments/{deployment_id}/revisions/{number}", response_model=s.RevisionOut)
def revision(store: StoreDep, deployment_id: str, number: int) -> s.RevisionOut:
    return queries.revision(store, deployment_id, number)


@router.get("/dependants", response_model=s.DependantsOut)
def dependants(
    store: StoreDep,
    cluster: str,
    type: Literal["tool", "model", "skill"],
    name: str,
) -> s.DependantsOut:
    """The agents that depend on a tool server backend (`type=tool`, `name` a backend name) or
    on a model alias (`type=model`, `name` an alias). An unknown name is an empty answer, not
    an error."""
    return queries.dependants(store, cluster, type, name)


@router.get("/status", response_model=s.StatusOut)
def status(store: StoreDep, settings: SettingsDep, at: Now, cluster: str) -> s.StatusOut:
    """Per agent: the owning path, the current release, blockers and next actions. Per tool
    server and model: what is declared, what was observed, and how old the observation is."""
    return queries.status(store, cluster, at, settings.observation_max_age_seconds)


@router.get("/events", response_model=s.EventsOut)
def events(
    store: StoreDep,
    deployment: str | None = None,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> s.EventsOut:
    return queries.events(store, deployment, after, limit)
