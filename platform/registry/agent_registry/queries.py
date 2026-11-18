"""What the read routes answer, as functions of the store. They take plain reads, never open a
transaction, and raise `Refusal("not-found", ...)` for a thing that does not exist."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from agent_registry import derive
from agent_registry.api import schemas as s
from agent_registry.models import Alias, Deployment, Observation, Release, Revision
from agent_registry.rules import Refusal, activation, revocation
from agent_registry.rules.revisions import candidate
from agent_registry.store import Reads

# ---- records ---------------------------------------------------------------------------------


def meta(store: Reads) -> s.MetaOut:
    found = store.meta()
    return s.MetaOut(
        mode=found.mode,
        mode_reason=found.mode_reason,
        mode_changed_at=found.mode_changed_at,
        generation=store.generation(),
    )


def items(store: Reads, kind: str | None = None) -> s.ItemsOut:
    return s.ItemsOut(items=store.items(kind))


def item(store: Reads, kind: str, name: str) -> s.ItemOut:
    found = store.item(kind, name)
    if found is None:
        raise Refusal("not-found", f"there is no {kind} item {name}")
    deployments = [d for d in store.deployments(kind=kind) if d.item == name]
    return s.ItemOut(
        item=found,
        releases=store.releases(kind, name),
        deployments=deployments,
        contracts={d.id: _contract_status(d) for d in deployments}
        if kind == "tool-server"
        else None,
        served_releases={d.id: d.document.get("release") for d in deployments}
        if kind == "model"
        else None,
    )


def _contract_status(server: Deployment) -> s.ContractStatus:
    contract = server.document.get("contract")
    if not contract:
        return s.ContractStatus(state="none")
    return s.ContractStatus(
        state="recorded", digest=contract["digest"], tools=len(contract["tools"])
    )


def release(store: Reads, release_id: str) -> s.ReleaseOut:
    found = store.release(release_id)
    if found is None:
        raise Refusal("not-found", f"there is no release {release_id}")
    return s.ReleaseOut(
        release=found,
        requirements=store.requirements(release_id),
        provisions=store.provisions(release_id),
    )


def deployments(
    store: Reads,
    kind: str | None = None,
    cluster: str | None = None,
    tenant: str | None = None,
    authority: str | None = None,
) -> s.DeploymentsOut:
    return s.DeploymentsOut(
        deployments=store.deployments(
            kind=kind, cluster=cluster, tenant=tenant, authority=authority
        )
    )


def _deployment(store: Reads, deployment_id: str) -> Deployment:
    found = store.deployment(deployment_id)
    if found is None:
        raise Refusal("not-found", f"there is no deployment {deployment_id}")
    return found


def deployment(store: Reads, deployment_id: str) -> s.DeploymentOut:
    found = _deployment(store, deployment_id)
    return s.DeploymentOut(
        deployment=found,
        revisions=store.revisions(found.id),
        approvals=store.approvals(found.id),
    )


def revision(store: Reads, deployment_id: str, number: int) -> s.RevisionOut:
    found = _deployment(store, deployment_id)
    stored = store.revision(found.id, number)
    if stored is None:
        raise Refusal("not-found", f"{found.id} has no revision {number}")
    return s.RevisionOut(
        revision=stored,
        release=store.release(stored.release_id),
        resolutions=store.resolutions(stored.id),
        approval=store.approval(stored.approval_id) if stored.approval_id else None,
        evaluations=activation.attempts_of(store, found.id, number),
    )


def events(
    store: Reads, deployment_id: str | None = None, after: int = 0, limit: int = 200
) -> s.EventsOut:
    # Events first: the generation read after them is never behind the newest one returned.
    found = store.events(deployment_id=deployment_id, after=after, limit=limit)
    return s.EventsOut(events=found, generation=store.generation())


# ---- dependants ------------------------------------------------------------------------------


def _looked_at(store: Reads, deployment: Deployment) -> tuple[Revision, Release] | None:
    """The revision an agent is judged by: the current one, or the newest when none is."""
    revisions = store.revisions(deployment.id)
    if not revisions:
        return None
    chosen = next((r for r in revisions if r.number == deployment.current_revision), revisions[-1])
    found = store.release(chosen.release_id)
    return (chosen, found) if found is not None else None


def _dependants(
    store: Reads, cluster: str, type: Literal["tool", "model"], target: str | None
) -> list[s.Dependant]:
    if target is None:
        return []
    found = []
    for deployment in sorted(store.deployments(kind="agent", cluster=cluster), key=lambda d: d.id):
        looked = _looked_at(store, deployment)
        if looked is None:
            continue
        chosen, release = looked
        for requirement in store.requirements(release.id, type=type, target=target):
            found.append(
                s.Dependant(
                    deployment=deployment.id,
                    agent=deployment.item,
                    tenant=deployment.tenant,
                    revision=chosen.number,
                    release=release.name,
                    names=list(requirement.names),
                )
            )
    return found


def _tool_server(store: Reads, cluster: str, backend: str) -> Deployment | None:
    for server in sorted(
        store.deployments(kind="tool-server", cluster=cluster), key=lambda d: d.id
    ):
        if server.document.get("registered") and server.document.get("backend") == backend:
            return server
    return None


def _alias(store: Reads, cluster: str, name: str) -> Alias | None:
    found = [a for a in store.aliases(cluster) if a.alias == name]
    found.sort(key=lambda a: (a.tenant is not None, a.tenant or ""))
    return found[0] if found else None


def dependants(
    store: Reads, cluster: str, type: Literal["tool", "model"], name: str
) -> s.DependantsOut:
    if type == "tool":
        server = _tool_server(store, cluster, name)
        target, provided_by = name, server.id if server else None
    else:
        alias = _alias(store, cluster, name)
        target, provided_by = (alias.backend, alias.deployment_id) if alias else (None, None)
    return s.DependantsOut(
        cluster=cluster,
        type=type,
        name=name,
        target=target,
        provided_by=provided_by,
        dependants=_dependants(store, cluster, type, target),
    )


# ---- status ----------------------------------------------------------------------------------


def _observed(observation: Observation | None, at: datetime, max_age: int) -> s.Observed:
    return s.Observed(
        state=derive.freshness(observation, at, max_age),
        observed_at=observation.observed_at if observation else None,
        age_seconds=derive.age_seconds(observation, at),
    )


def _projection(store: Reads, cluster: str, deployment: Deployment, kind: str, name: str):
    """(result, message) of the provisioner's last attempt at an object, when it is in the
    store and readable. A stored result of another shape is not an answer."""
    stored = store.observation(cluster, "projection", deployment.namespace, f"{kind}/{name}")
    document = stored.document if stored else None
    if not isinstance(document, dict):
        return None, stored
    result = document.get("result")
    return (result if isinstance(result, str) else None), stored


PENDING_TOO_LONG_SECONDS = 600


def _refused_at(store: Reads, deployment_id: str, number: int) -> datetime | None:
    """When a revision was refused: its newest `revision-refused` event."""
    found, after = None, 0
    while True:
        page = store.events(deployment_id=deployment_id, after=after, limit=1000)
        for event in page:
            if event.type == "revision-refused" and event.detail.get("revision") == number:
                found = event.at
        if len(page) < 1000:
            return found
        after = page[-1].seq


def _delivery_status(
    store: Reads,
    deployment: Deployment,
    revisions: list[Revision],
    releases: dict[int, Release | None],
    at: datetime,
    agent_observation: Observation | None,
    agent_state: str,
    blockers: list[str],
    actions: list[str],
) -> dict[str, Any]:
    """The fields of `contract/delivery.md` section 10, and the blockers and actions they add."""
    named = {r.number: releases[r.number].name for r in revisions if releases[r.number]}
    registry = deployment.authority == "registry"

    current = None
    admission = None
    if deployment.current_revision is not None:
        number = deployment.current_revision
        stored = next((r for r in revisions if r.number == number), None)
        if stored is not None and number in named:
            admission = activation.admission(store, deployment.id, number)
            current = s.CurrentStatus(
                release=named[number],
                revision=number,
                since=stored.activated_at,
                admitted_by=admission.id if admission else None,
            )

    waiting = candidate(revisions)
    candidacy = None
    if waiting is not None and waiting.number in named:
        attempts = activation.attempts_of(store, deployment.id, waiting.number)
        last = attempts[-1] if attempts else None
        candidacy = s.CandidateStatus(
            release=named[waiting.number],
            revision=waiting.number,
            phase=waiting.phase,
            reason=waiting.reason,
            evaluation=s.CandidateEvaluation(attempt=last.attempt, state=last.state)
            if last
            else None,
        )
        if last is not None and last.state == "pending":
            if (at - last.created_at).total_seconds() > PENDING_TOO_LONG_SECONDS:
                blockers.append(
                    f"evaluation {last.id} has been pending for more than 10 minutes; "
                    "the evaluator is not running"
                )

    refused = None
    newest = revisions[-1] if revisions else None
    if (
        newest is not None
        and newest.phase == "refused"
        and newest.number > (deployment.current_revision or 0)
        and newest.number in named
    ):
        attempts = activation.attempts_of(store, deployment.id, newest.number)
        verdict = attempts[-1] if attempts and attempts[-1].state in ("failed", "error") else None
        refused = s.RefusedStatus(
            release=named[newest.number],
            revision=newest.number,
            reason=newest.reason,
            at=_refused_at(store, deployment.id, newest.number),
            evaluation=s.RefusedEvaluation(
                verdict=verdict.state, summary=(verdict.result or {}).get("summary", "")
            )
            if verdict
            else None,
        )
        blockers.append(f"release {named[newest.number]} was refused: {newest.reason}")
        actions.append(
            "release a fixed version"
            + (" or ask for another evaluation" if refused.evaluation else "")
        )

    revoked = [
        s.RevokedStatus(id=r.id, release=r.release, reason=r.reason, at=r.revoked_at)
        for r in sorted(
            store.revocations(kind="agent", item=deployment.item),
            key=lambda r: (r.revoked_at, r.id),
            reverse=True,
        )
    ]
    if registry and current is None and deployment.current_revision is None and revoked:
        if any(rel is not None and revocation.revoked(store, rel) for rel in releases.values()):
            blockers.append(
                f"there is no current release: {revoked[0].release} was revoked ({revoked[0].id})"
            )
            actions.append(f"just agent rollback {deployment.item}")

    acknowledged = None
    if registry:
        live = (
            derive.live_generation(agent_observation.document)
            if agent_observation is not None
            else None
        )
        acknowledged = agent_state == "fresh" and live is not None and live >= deployment.generation
        if agent_state == "fresh" and not acknowledged:
            blockers.append("the cluster has not taken the last change to the Agent")

    return {
        "current": current,
        "candidate": candidacy,
        "last_refused": refused,
        "acknowledged": acknowledged,
        "pinned": deployment.pinned_revision,
        "revoked": revoked,
    }


def _agent_status(
    store: Reads, deployment: Deployment, *, mode: str, at: datetime, max_age: int
) -> s.AgentStatus:
    cluster, namespace = deployment.cluster, deployment.namespace
    revisions = store.revisions(deployment.id)
    newest = revisions[-1] if revisions else None
    current = next((r for r in revisions if r.number == deployment.current_revision), None)
    releases = {r.number: store.release(r.release_id) for r in revisions}

    def release_name(revision: Revision | None) -> str | None:
        return releases[revision.number].name if revision else None

    agent_observation = store.observation(cluster, "agent", namespace, deployment.item)
    agent_state = derive.freshness(agent_observation, at, max_age)

    ready = None
    current_release_state = None
    if current is not None:
        observation = store.observation(
            cluster, "agentrelease", namespace, release_name(current) or ""
        )
        current_release_state = derive.freshness(observation, at, max_age)
        if current_release_state == "fresh":
            ready = derive.condition(observation.document, "Ready").get("status") == "True"
            not_ready = derive.condition(observation.document, "Ready").get("reason") or ""

    blockers: list[str] = []
    actions: list[str] = []

    if mode == "recovery" and deployment.authority == "registry":
        blockers.append("the registry is in recovery; nothing is being changed")
    if agent_state == "missing":
        blockers.append("the cluster has never reported this agent")
    elif agent_state == "old":
        blockers.append(
            f"the cluster last reported this agent {derive.age_seconds(agent_observation, at)} s ago"
        )
    elif agent_state == "absent":
        blockers.append("the agent is not on the cluster")
    if not deployment.enabled:
        blockers.append("the agent is disabled")

    objects = [("agent", deployment.item, "Agent")] + [
        ("agentrelease", releases[r.number].name, "AgentRelease")
        for r in revisions
        if releases[r.number] is not None
    ]
    for lower, name, kind in objects:
        result, stored = _projection(store, cluster, deployment, lower, name)
        if derive.freshness(stored, at, max_age) != "fresh":
            continue
        if result == "refused-flux-inventory":
            blockers.append(f"Flux still owns {kind}/{name}")
            actions.append(
                f"merge the pull request that removes {deployment.item} from the Flux kustomization"
            )
        elif result == "refused-older":
            blockers.append(f"the cluster holds a newer {kind}/{name} than the registry")
        elif result == "error":
            message = stored.document.get("message")
            blockers.append(f"{kind}/{name} could not be written: {message}")

    extra = _delivery_status(
        store,
        deployment,
        revisions,
        releases,
        at,
        agent_observation,
        agent_state,
        blockers,
        actions,
    )

    if newest is not None:
        name = release_name(newest)
        if newest.phase == "evaluating":
            blockers.append(f"release {name}: {newest.reason}")
        elif newest.phase == "awaiting-runtime":
            blockers.append(f"release {name}: {newest.reason}")
            if newest.approval_id is None and deployment.authority == "pull-request":
                actions.append(f"just agent approve {name}")
        elif newest.phase == "pending-approval":
            blockers.append(f"release {name} waits for approval: {newest.reason}")
            actions.append(f"just agent approve {deployment.item}")

    if deployment.pinned_revision is not None:
        later = [
            r.number
            for r in revisions
            if r.number > deployment.pinned_revision and r.phase == "activated"
        ]
        if later:
            blockers.append(
                f"pinned to revision {deployment.pinned_revision}; revision {max(later)} "
                "is activated and not in use"
            )

    if current is not None and ready is False:
        blockers.append(f"current release {release_name(current)} is not Ready: {not_ready}")

    return s.AgentStatus(
        deployment=deployment.id,
        agent=deployment.item,
        tenant=deployment.tenant,
        namespace=namespace,
        authority=deployment.authority,
        enabled=deployment.enabled,
        pinned_revision=deployment.pinned_revision,
        current_revision=deployment.current_revision,
        current_release=release_name(current),
        newest_revision=newest.number if newest else None,
        newest_release=release_name(newest),
        phase=newest.phase if newest else None,
        ready=ready,
        blockers=blockers,
        next_actions=actions,
        observation=_observed(agent_observation, at, max_age),
        **extra,
    )


def _agent_names(store: Reads, cluster: str, targets: set[str], type: str) -> list[str]:
    names: set[str] = set()
    for target in targets:
        names.update(d.agent for d in _dependants(store, cluster, type, target))
    return sorted(names)


def _list(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def status(store: Reads, cluster: str, now: datetime, max_age_seconds: int) -> s.StatusOut:
    if store.cluster(cluster) is None:
        raise Refusal("not-found", f"there is no cluster {cluster}")
    mode = store.meta().mode
    agents = [
        _agent_status(store, d, mode=mode, at=now, max_age=max_age_seconds)
        for d in sorted(store.deployments(kind="agent", cluster=cluster), key=lambda d: d.id)
    ]

    servers = []
    for server in sorted(
        store.deployments(kind="tool-server", cluster=cluster), key=lambda d: d.id
    ):
        document = server.document
        stored = store.observation(cluster, "tool-server", server.namespace, server.item)
        fresh = derive.freshness(stored, now, max_age_seconds) == "fresh"
        backend = document.get("backend")
        registered = bool(document.get("registered"))
        servers.append(
            s.ToolServerStatus(
                deployment=server.id,
                name=server.item,
                namespace=server.namespace,
                registered=registered,
                backend=backend,
                declared_image=document.get("image") or "",
                pinned_by_digest=bool(document.get("pinnedByDigest")),
                observed_images=_list(stored.document.get("images")) if fresh else [],
                ready_replicas=stored.document.get("readyReplicas") if fresh else None,
                observation=_observed(stored, now, max_age_seconds),
                dependants=_agent_names(
                    store, cluster, {backend} if registered and backend else set(), "tool"
                ),
                contract=_contract_status(server),
            )
        )

    aliases = store.aliases(cluster)
    engines = []
    for model in sorted(store.deployments(kind="model", cluster=cluster), key=lambda d: d.id):
        document = model.document
        stored = store.observation(cluster, "model-engine", model.namespace, model.item)
        fresh = derive.freshness(stored, now, max_age_seconds) == "fresh"
        mine = [a for a in aliases if a.deployment_id == model.id]
        backends = {a.backend for a in mine} | (
            {document["backend"]} if document.get("backend") else set()
        )
        images = _list(stored.document.get("images")) if fresh else []
        served = stored.document.get("servedName") if fresh else None
        engines.append(
            s.ModelStatus(
                deployment=model.id,
                name=model.item,
                namespace=model.namespace,
                aliases=sorted({a.alias for a in mine}),
                backend=document.get("backend"),
                declared_served_name=document.get("servedName") or "",
                observed_served_name=served if isinstance(served, str) else None,
                observed_image=images[0] if images else None,
                observation=_observed(stored, now, max_age_seconds),
                dependants=_agent_names(store, cluster, backends, "model"),
                release=document.get("release"),
            )
        )

    return s.StatusOut(
        cluster=cluster,
        mode=mode,
        generation=store.generation(),
        observation_max_age_seconds=max_age_seconds,
        agents=agents,
        tool_servers=servers,
        models=engines,
    )
