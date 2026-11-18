"""Which releases are revoked (`contract/delivery.md` section 8).

`revoked` is the one function other rules call; its signature is fixed. Import this module,
not its names, from `rules.revisions`: the two modules call each other.
"""

from __future__ import annotations

from datetime import datetime

from agent_registry.models import Release, Revocation, release_id
from agent_registry.rules import revisions
from agent_registry.store import Reads, Tx


def revoked(reads: Reads, release: Release) -> Revocation | None:
    """The revocation of this release, or None. The oldest, when there are several."""
    return revoked_named(reads, release.kind, release.item, release.name)


def revoked_named(reads: Reads, kind: str, item: str, name: str) -> Revocation | None:
    """The same, for a release not yet recorded, named by its kind, item and release name."""
    for revocation in reads.revocations(kind=kind, item=item):
        if revocation.release == name:
            return revocation
    return None


def apply(tx: Tx, revocation: Revocation, *, now: datetime) -> list[str]:
    """Write the event `release-revoked` and, for an `agent` revocation, its effects on every
    registry-owned deployment with a revision of the release (section 8.2). The revocation
    is already stored. Returns the deployments changed. On the pull request path, and for
    other kinds, nothing but the event is written."""
    seq = tx.event(
        "release-revoked",
        detail={
            "id": revocation.id,
            "kind": revocation.kind,
            "item": revocation.item,
            "release": revocation.release,
            "reason": revocation.reason,
            "sessions": revocation.sessions,
        },
    ).seq
    if revocation.kind != "agent":
        return []
    target = release_id(revocation.kind, revocation.item, revocation.release)
    changed = []
    for deployment in sorted(
        tx.deployments(kind="agent", authority="registry"), key=lambda d: d.id
    ):
        if deployment.item != revocation.item:
            continue
        hit = [r for r in tx.revisions(deployment.id) if r.release_id == target]
        if not hit:
            continue
        last = seq
        for revision in hit:
            reason = f"release revoked: {revocation.id}"
            if revision.phase in revisions.OPEN_PHASES:
                tx.set_revision_progress(
                    deployment.id, revision.number, phase="refused", reason=reason, generation=seq
                )
                revisions.cancel_open_evaluations(tx, deployment.id, revision.number, now=now)
                tx.event(
                    "revision-refused",
                    deployment_id=deployment.id,
                    detail={"revision": revision.number, "reason": reason},
                )
            else:
                tx.set_revision_progress(deployment.id, revision.number, generation=seq)
        current = tx.deployment(deployment.id)
        assert current is not None
        numbers = {r.number for r in hit}
        if current.current_revision in numbers:
            last = tx.event(
                "current-cleared",
                deployment_id=deployment.id,
                detail={"revision": current.current_revision, "revocation": revocation.id},
            ).seq
            update = {"current_revision": None, "generation": last}
            if current.pinned_revision in numbers:
                update["pinned_revision"] = None
            tx.put_deployment(current.model_copy(update=update))
        revisions.settle_agent(tx, deployment.id, last)
        changed.append(deployment.id)
    return changed
