"""The cluster routes (package 1.4b). Caller: the provisioner of the named cluster only.

Rules: `contract/projection.md`. Logic lives in `agent_registry/rules/projection.py` and
`agent_registry/rules/intake.py`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from agent_registry import modes
from agent_registry.api import schemas as s
from agent_registry.api.auth import Principal, provisioner
from agent_registry.api.deps import get_settings, get_store, now
from agent_registry.config import FIELD_MANAGER, Settings
from agent_registry.models import Observation
from agent_registry.rules import Refusal, intake, projection
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
    if store.cluster(cluster) is None:
        raise Refusal("not-found", f"there is no cluster {cluster}")
    registry = store.generation()
    if last_generation > registry:
        modes.enter_recovery(
            store, intake.recovery_reason(cluster, last_generation, registry), actor=who.actor
        )
    meta = store.meta()
    normal = meta.mode == "normal"
    deployments = []
    for deployment in projection.registry_owned(store, cluster) if normal else []:
        deployments.append(
            s.DesiredDeployment(
                deployment=deployment.id,
                agent=deployment.item,
                tenant=deployment.tenant or "",
                namespace=deployment.namespace,
                objects=[
                    s.ProjectedObject(
                        kind=o.kind,
                        namespace=o.namespace,
                        name=o.name,
                        generation=o.generation,
                        document=o.document,
                    )
                    for o in projection.objects_of(store, deployment)
                ],
            )
        )
    return s.DesiredOut(
        cluster=cluster,
        mode=meta.mode,
        generation=store.generation(),
        field_manager=FIELD_MANAGER,
        namespaces=projection.namespaces_of(store, cluster),
        flux_inventories=store.cluster(cluster).flux_inventories,
        deployments=deployments,
        observe=[
            s.ObserveSubject(
                subject_kind=o.subject_kind,
                name=o.name,
                namespace=o.namespace,
                workload=s.ObjectRef(**o.workload),
                served_name_from=o.served_name_from,
            )
            for o in projection.observe_of(store, cluster)
        ],
    )


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
    if store.cluster(cluster) is None:
        raise Refusal("not-found", f"there is no cluster {cluster}")
    records = [
        Observation(
            cluster=cluster,
            subject_kind=o.subject_kind,
            namespace=o.namespace,
            name=o.name,
            observed_at=body.observed_at,
            in_flux_inventory=o.in_flux_inventory,
            document=o.document,
        )
        for o in body.objects
    ]
    records += [
        Observation(
            cluster=cluster,
            subject_kind=o.subject_kind,
            namespace=o.namespace,
            name=o.name,
            observed_at=body.observed_at,
            present=o.present,
            document=o.document,
        )
        for o in body.subjects
    ]
    records += [
        Observation(
            cluster=cluster,
            subject_kind="projection",
            namespace=p.namespace,
            name=f"{p.kind.lower()}/{p.name}",
            observed_at=body.observed_at,
            document={
                "deployment": p.deployment,
                "generation": p.generation,
                "result": p.result,
                "message": p.message,
            },
        )
        for p in body.projections
    ]
    mode, generation, stored = intake.accept(
        store,
        cluster,
        observations=records,
        complete_for=[(c.subject_kind, c.namespace) for c in body.complete],
        objects=len(body.objects),
        observed_at=body.observed_at,
        last_generation=body.last_generation,
        now=at,
        max_age_seconds=settings.observation_max_age_seconds,
        actor=who.actor,
    )
    return s.ObservationsOut(mode=mode, generation=generation, stored=stored)
