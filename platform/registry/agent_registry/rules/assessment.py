"""What moving an alias would touch (`contract/dependencies.md` section 5). Functions of plain
reads, apart from `create`, which records one in a transaction."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agent_registry import derive
from agent_registry.models import Alias, Assessment, Deployment, Release, Revision
from agent_registry.models import deployment_id as deployment_id_of
from agent_registry.models import release_id as release_id_of
from agent_registry.rules import Refusal, activation
from agent_registry.store import Reads, Tx

NEED_TOOL_CALLING = "toolCalling"


def find_alias(reads: Reads, cluster: str, name: str) -> Alias | None:
    """The alias of a cluster by name; a cluster-wide one before a tenant's."""
    found = [a for a in reads.aliases(cluster) if a.alias == name]
    found.sort(key=lambda a: (a.tenant is not None, a.tenant or ""))
    return found[0] if found else None


def pair_behind(reads: Reads, alias: Alias) -> dict[str, str] | None:
    """`{engine, release}` of an alias: the engine it points at and the release that engine
    serves. None when the engine is not stored."""
    engine = reads.deployment(alias.deployment_id)
    if engine is None or "servedName" not in engine.document:
        return None
    return {"engine": engine.item, "release": derive.engine_release(engine.document)}


def pairs(reads: Reads) -> dict[tuple[str, str], dict[str, str]]:
    """The pair behind every alias, by cluster and alias name."""
    found = {}
    for alias in reads.aliases():
        pair = pair_behind(reads, alias)
        if pair is not None:
            found[(alias.cluster, alias.alias)] = pair
    return found


def looked_at(reads: Reads, deployment: Deployment) -> list[Revision]:
    """The revisions an agent's dependency is judged by: the current one, and the newest one
    when it is neither refused nor superseded."""
    revisions = reads.revisions(deployment.id)
    found = [r for r in revisions if r.number == deployment.current_revision]
    if revisions and revisions[-1].phase not in ("refused", "superseded"):
        if revisions[-1] not in found:
            found.append(revisions[-1])
    return found


def uses_alias(reads: Reads, revisions: list[Revision], alias: str) -> bool:
    return any(
        row.type == "model" and row.detail.get("alias") == alias
        for revision in revisions
        for row in reads.resolutions(revision.id)
    )


def dependants_of(reads: Reads, cluster: str, alias: str) -> list[Deployment]:
    """The agent deployments of a cluster, of either authority, that use the alias."""
    return [
        d
        for d in sorted(reads.deployments(kind="agent", cluster=cluster), key=lambda d: d.id)
        if uses_alias(reads, looked_at(reads, d), alias)
    ]


def _dependant(reads: Reads, deployment: Deployment, offers: dict[str, Any]) -> dict[str, Any]:
    revisions = looked_at(reads, deployment)
    current = next((r for r in revisions if r.number == deployment.current_revision), None)
    judged = current or revisions[-1]
    named = reads.release(current.release_id) if current else None
    release: Release | None = named or reads.release(judged.release_id)
    assert release is not None  # a revision always has its release
    needs = []
    if reads.requirements(release.id, type="tool"):
        met = offers.get(NEED_TOOL_CALLING)
        needs.append({"need": NEED_TOOL_CALLING, "met": met if isinstance(met, bool) else None})
    registry = deployment.authority == "registry"
    return {
        "deployment": deployment.id,
        "agent": deployment.item,
        "tenant": deployment.tenant,
        "authority": deployment.authority,
        "enabled": deployment.enabled,
        "release": named.name if named else None,
        "held": registry and deployment.enabled,
        "evaluation": "required" if activation.required(release) else "none",
        "needs": needs,
    }


def compute(reads: Reads, cluster: str, alias: str, candidate: dict[str, str]) -> dict[str, Any]:
    """The fields of an assessment other than id, author and time. `not-found` for an unknown
    alias; an unknown candidate release only means its manifest offers nothing."""
    found = find_alias(reads, cluster, alias)
    pair = pair_behind(reads, found) if found else None
    if found is None or pair is None:
        raise Refusal("not-found", f"there is no alias {alias} on cluster {cluster}")
    manifest = reads.release(release_id_of("model", candidate["engine"], candidate["release"]))
    offers = (manifest.document.get("offers") if manifest else None) or {}
    dependants = [_dependant(reads, d, offers) for d in dependants_of(reads, cluster, alias)]
    reduced = [
        {k: d[k] for k in ("deployment", "authority", "enabled", "release")} for d in dependants
    ]
    return {
        "pair": pair,
        "candidate": candidate,
        "dependants": dependants,
        "blocked": any(n["met"] is False for d in dependants if d["enabled"] for n in d["needs"]),
        "digest": derive.sha(
            {
                "cluster": cluster,
                "alias": alias,
                "from": pair,
                "candidate": candidate,
                "dependants": reduced,
            }
        ),
    }


def create(
    tx: Tx,
    assessment_id: str,
    cluster: str,
    alias: str,
    candidate: dict[str, str],
    *,
    actor: str,
    now: datetime,
) -> Assessment:
    """Compute and store an assessment. The candidate must be a stored release of a stored
    engine and must differ from the pair behind the alias now."""
    if tx.cluster(cluster) is None:
        raise Refusal("not-found", f"there is no cluster {cluster}")
    engine, name = candidate["engine"], candidate["release"]
    if tx.deployment(deployment_id_of("model", cluster, None, engine)) is None:
        raise Refusal("not-found", f"there is no model engine {engine} on cluster {cluster}")
    try:
        stored = tx.release(release_id_of("model", engine, name))
    except ValueError:
        stored = None
    if stored is None:
        raise Refusal("not-found", f"there is no release {name} of engine {engine}")
    fields = compute(tx, cluster, alias, {"engine": engine, "release": name})
    if fields["pair"] == fields["candidate"]:
        raise Refusal("invalid-arguments", f"{engine}/{name} is what alias {alias} serves now")
    record = Assessment(
        id=assessment_id,
        cluster=cluster,
        alias=alias,
        created_by=actor,
        created_at=now,
        **fields,
    )
    tx.event(
        "assessment-created",
        detail={"assessment": record.id, "alias": alias, "candidate": record.candidate},
    )
    tx.add_assessment(record)
    return record


def changes(stored: Assessment, fresh: dict[str, Any]) -> list[str]:
    """What differs between a stored assessment and the same one computed now, in words.
    Empty when the digests agree."""
    if stored.digest == fresh["digest"]:
        return []
    found = []
    if stored.pair != fresh["pair"]:
        was, now = stored.pair, fresh["pair"]
        found.append(
            f"the alias moved from {was['engine']}/{was['release']} "
            f"to {now['engine']}/{now['release']}"
        )
    before = {d["deployment"]: d for d in stored.dependants}
    after = {d["deployment"]: d for d in fresh["dependants"]}
    for name in sorted(after.keys() - before.keys()):
        found.append(f"{name} now depends on the alias")
    for name in sorted(before.keys() - after.keys()):
        found.append(f"{name} no longer depends on the alias")
    for name in sorted(before.keys() & after.keys()):
        for key in ("authority", "enabled", "release"):
            if before[name][key] != after[name][key]:
                found.append(
                    f"{name}: {key} changed from {before[name][key]!r} to {after[name][key]!r}"
                )
    return found or ["the alias no longer matches what was assessed"]


def staleness(reads: Reads, stored: Assessment) -> list[str]:
    """Why a stored assessment is stale, by computing it again; [] when it is not."""
    try:
        fresh = compute(reads, stored.cluster, stored.alias, stored.candidate)
    except Refusal:
        return [f"alias {stored.alias} no longer exists"]
    return changes(stored, fresh)


def is_stale(reads: Reads, stored: Assessment) -> bool:
    return bool(staleness(reads, stored))
