"""Recovery: what differs between the registry and the clusters, and adopting it when
recovery is left (`contract/projection.md` section 8)."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from agent_registry import canonical, derive
from agent_registry.config import ANNOTATION_PREFIX
from agent_registry.models import (
    Approval,
    Deployment,
    Observation,
    Revision,
    revision_id,
)
from agent_registry.rules import Refusal, projection
from agent_registry.rules.lifecycle import API_VERSION
from agent_registry.store import Reads, Tx

RECOVERED_UNAPPROVED = "recovered unapproved"


@dataclass(frozen=True)
class Difference:
    deployment: str
    subject: str  # "agent/<name>" or "agentrelease/<name>"
    field: str
    registry: Any
    cluster: Any


@dataclass(frozen=True)
class Report:
    mode: str
    reason: str
    complete: bool
    waiting_for: list[str]
    differences: list[Difference]
    digest: str


def _digest_of(differences: list[Difference]) -> str:
    return canonical.digest([asdict(d) for d in differences])


def _without_enabled(spec: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in spec.items() if k != "enabled"}


def _present(reads: Reads, deployment: Deployment, kind: str, name: str) -> Observation | None:
    found = reads.observation(deployment.cluster, kind, deployment.namespace, name)
    return found if found is not None and found.present else None


def _annotation(observation: Observation, key: str) -> str | None:
    annotations = (observation.document.get("metadata") or {}).get("annotations") or {}
    return annotations.get(ANNOTATION_PREFIX + key)


def _release_names(reads: Reads, deployment: Deployment) -> dict[str, Revision]:
    names = {}
    for revision in reads.revisions(deployment.id):
        release = reads.release(revision.release_id)
        assert release is not None
        names[release.name] = revision
    return names


def _unknown_releases(
    reads: Reads, deployment: Deployment, known: dict[str, Revision]
) -> list[Observation]:
    return [
        o
        for o in reads.observations(
            deployment.cluster, subject_kind="agentrelease", namespace=deployment.namespace
        )
        if o.present
        and (o.document.get("spec") or {}).get("agent") == deployment.item
        and o.name not in known
    ]


def differences_of(reads: Reads, deployment: Deployment) -> list[Difference]:
    """The differences of section 8 for one registry-owned agent, from its last observations."""
    found: list[Difference] = []

    def add(subject: str, field: str, registry: Any, cluster: Any) -> None:
        found.append(Difference(deployment.id, subject, field, registry, cluster))

    subject = f"agent/{deployment.item}"
    live = _present(reads, deployment, "agent", deployment.item)
    if live is not None:
        live_spec = live.document.get("spec") or {}
        projected = projection.agent_spec(deployment)
        if _without_enabled(live_spec) != _without_enabled(projected):
            add(
                subject,
                "agent",
                canonical.digest(_without_enabled(projected)),
                canonical.digest(_without_enabled(live_spec)),
            )
        if live_spec.get("enabled", True) != projected["enabled"]:
            add(subject, "enabled", projected["enabled"], live_spec.get("enabled", True))
        named = _annotation(live, "current-release")
        current = (
            reads.revision(deployment.id, deployment.current_revision)
            if deployment.current_revision is not None
            else None
        )
        release = reads.release(current.release_id) if current else None
        if named is not None and named != (release.name if release else None):
            add(subject, "current-release", release.name if release else None, named)
        generation = derive.live_generation(live.document)
        if generation is not None and generation > deployment.generation:
            add(subject, "generation", deployment.generation, generation)

    known = _release_names(reads, deployment)
    for name, revision in known.items():
        observed = _present(reads, deployment, "agentrelease", name)
        if observed is None:
            continue
        subject = f"agentrelease/{name}"
        shown = (observed.document.get("spec") or {}).get("approval") or {}
        block = projection.approval_block(reads, revision)
        if shown.get("approved") is True and (revision.approval_id is None or block != shown):
            add(subject, "approval", block, shown)
        generation = derive.live_generation(observed.document)
        if generation is not None and generation > revision.generation:
            add(subject, "generation", revision.generation, generation)
    for observed in _unknown_releases(reads, deployment, known):
        add(f"agentrelease/{observed.name}", "release", None, observed.name)

    return found


def report(reads: Reads, *, now: datetime, max_age_seconds: int) -> Report:
    meta = reads.meta()
    if meta.mode != "recovery":
        return Report("normal", "", True, [], [], _digest_of([]))
    owned = reads.deployments(kind="agent", authority="registry")
    differences = sorted(
        (d for deployment in owned for d in differences_of(reads, deployment)),
        key=lambda d: (d.deployment, d.subject, d.field),
    )
    waiting = []
    for cluster in sorted({d.cluster for d in owned}):
        heard = reads.observation(cluster, "projection", "", "report")
        if (
            derive.freshness(heard, now, max_age_seconds) != "fresh"
            or meta.mode_changed_at is not None
            and heard.observed_at < meta.mode_changed_at
        ):
            waiting.append(cluster)
    return Report(
        "recovery",
        meta.mode_reason,
        not waiting,
        waiting,
        differences,
        _digest_of(differences),
    )


# -- leaving -------------------------------------------------------------------------------


def _highest_seen(tx: Reads, owned: list[Deployment]) -> int:
    """The highest generation any cluster shows or says it has seen."""
    top = 0
    for cluster in tx.clusters():
        heard = tx.observation(cluster.name, "projection", "", "report")
        if heard is not None:
            top = max(top, int(heard.document.get("lastGeneration") or 0))
    for name in sorted({d.cluster for d in owned}):
        for kind in ("agent", "agentrelease"):
            for observed in tx.observations(name, subject_kind=kind):
                if observed.present:
                    top = max(top, derive.live_generation(observed.document) or 0)
    return top


def _time(text: Any, fallback: datetime) -> datetime:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return fallback


def _approve_as_found(
    tx: Tx, deployment: Deployment, revision: Revision, block: dict[str, Any], now: datetime
) -> None:
    """Record the approval the cluster shows for a revision and attach it."""
    approval = Approval(
        id=f"recovered:{deployment.id}@{revision.number}",
        deployment_id=deployment.id,
        revision=revision.number,
        authority_digest=revision.authority_digest,
        base_authority_digest="",
        policy_revision=revision.policy_revision,
        approved_by=str(block.get("approvedBy") or ""),
        approved_at=_time(block.get("approvedAt"), now),
        ticket=block.get("ticket"),
        origin="recovered",
        evidence={"approval": copy.deepcopy(block)},
    )
    tx.add_approval(approval)
    if revision.approval_id is not None and revision.approval_id != approval.id:
        tx.set_approval_state(revision.approval_id, "superseded")
    tx.set_revision_progress(
        deployment.id,
        revision.number,
        phase="activated",
        approval_id=approval.id,
        activated_at=revision.activated_at or now,
    )


def _recover_release(
    tx: Tx, deployment: Deployment, observed: Observation, agent: dict[str, Any], now: datetime
) -> None:
    """A release the cluster holds and the registry does not: record it as the next revision."""
    spec = copy.deepcopy(observed.document.get("spec") or {})
    block = spec.get("approval") or {}
    document = {
        "apiVersion": API_VERSION,
        "kind": "AgentRelease",
        "metadata": {"name": observed.name, "namespace": observed.namespace},
        "spec": spec,
    }
    release = derive.agent_release_record(document, recorded_at=now)
    requirements = derive.requirements_of(release.id, agent)
    authority = canonical.authority_of(agent)
    number = max((r.number for r in tx.revisions(deployment.id)), default=0) + 1
    tenant = tx.tenant(deployment.cluster, deployment.tenant or "")
    approved = block.get("approved") is True
    tx.add_release(release)
    tx.add_requirements(release.id, requirements)
    tx.add_revision(
        Revision(
            id=revision_id(deployment.id, number),
            deployment_id=deployment.id,
            number=number,
            release_id=release.id,
            origin="recovered",
            agent_document=agent,
            authority=authority,
            authority_digest=canonical.authority_digest(authority),
            policy_revision=tenant.policy_revision if tenant else "",
            created_at=now,
            phase="awaiting-runtime",
            reason="" if approved else RECOVERED_UNAPPROVED,
        )
    )
    tx.add_resolutions(
        revision_id(deployment.id, number),
        derive.resolutions_of(tx, deployment.cluster, deployment.id, number, requirements),
    )
    if approved:
        revision = tx.revision(deployment.id, number)
        assert revision is not None
        _approve_as_found(tx, deployment, revision, block, now)


def _adopt(
    tx: Tx, deployment: Deployment, differences: list[Difference], seq: int, now: datetime
) -> None:
    """Make the registry's records of one agent equal what its cluster holds."""
    live = _present(tx, deployment, "agent", deployment.item)
    fields = {d.field for d in differences if d.subject.startswith("agent/")}
    agent = deployment.document["agent"]
    update: dict[str, Any] = {}
    if live is not None and fields & {"agent", "enabled"}:
        live_spec = copy.deepcopy(live.document.get("spec") or {})
        # Disabled is the deployment's flag; leaving it False in the document too would
        # make a later enable project nothing.
        spec = live_spec if "agent" in fields else copy.deepcopy(agent.get("spec") or {})
        if "enabled" in fields:
            update["enabled"] = live_spec.get("enabled", True)
            spec["enabled"] = True
        else:
            spec["enabled"] = (agent.get("spec") or {}).get("enabled", True)
        agent = {**agent, "spec": spec}
        update["document"] = {**deployment.document, "agent": agent}

    live_agent = agent
    if live is not None:
        live_agent = canonical.agent_document(
            {
                "apiVersion": agent.get("apiVersion"),
                "kind": "Agent",
                "metadata": {"name": deployment.item, "namespace": deployment.namespace},
                "spec": live.document.get("spec"),
            }
        )

    for found in sorted((d for d in differences if d.field == "release"), key=lambda d: d.subject):
        observed = _present(tx, deployment, "agentrelease", found.cluster)
        assert observed is not None
        _recover_release(tx, deployment, observed, live_agent, now)

    by_name = _release_names(tx, deployment)
    for found in differences:
        if found.field != "approval":
            continue
        name = found.subject.partition("/")[2]
        observed = _present(tx, deployment, "agentrelease", name)
        assert observed is not None
        _approve_as_found(
            tx, deployment, by_name[name], (observed.document.get("spec") or {})["approval"], now
        )

    for found in differences:
        if found.field == "current-release" and found.cluster in by_name:
            update["current_revision"] = by_name[found.cluster].number

    for revision in tx.revisions(deployment.id):
        tx.set_revision_progress(deployment.id, revision.number, generation=seq)
    current = tx.deployment(deployment.id)
    assert current is not None
    tx.put_deployment(current.model_copy(update=update | {"generation": seq}))


def leave(
    tx: Tx, digest: str, *, now: datetime, max_age_seconds: int
) -> tuple[int, list[Difference]]:
    """Adopt what the clusters hold and return to normal. Returns the generation and the
    differences adopted."""
    if tx.meta().mode != "recovery":
        raise Refusal("conflict", "the registry is not in recovery")
    found = report(tx, now=now, max_age_seconds=max_age_seconds)
    if not found.complete:
        raise Refusal(
            "conflict",
            "the report is not complete; waiting for a fresh report from "
            + ", ".join(found.waiting_for),
        )
    if digest != found.digest:
        raise Refusal("conflict", "the differences are not the ones named; read them again")

    owned = sorted(tx.deployments(kind="agent", authority="registry"), key=lambda d: d.id)
    tx.advance_generation(_highest_seen(tx, owned))
    seq = tx.event(
        "recovery-left", detail={"digest": digest, "adopted": len(found.differences)}
    ).seq
    for deployment in owned:
        _adopt(
            tx,
            deployment,
            [d for d in found.differences if d.deployment == deployment.id],
            seq,
            now,
        )
    tx.set_mode("normal", "")
    return tx.generation(), found.differences
