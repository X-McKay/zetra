"""The in-memory store. Every package but the PostgreSQL store tests against this one."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_registry.models import (
    Alias,
    Approval,
    ApprovalState,
    Cluster,
    Deployment,
    Evaluation,
    EvaluationState,
    Event,
    Item,
    Meta,
    Mode,
    Observation,
    Phase,
    Provision,
    Release,
    Requirement,
    Resolution,
    Revision,
    Revocation,
    Tenant,
    TrustedRepository,
)
from agent_registry.store import Conflict, Missing, StoreError, Unevented, completion_is_new


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _State:
    meta: Meta = field(default_factory=Meta)
    seq: int = 0
    clusters: dict[str, Cluster] = field(default_factory=dict)
    tenants: dict[tuple[str, str], Tenant] = field(default_factory=dict)
    repositories: dict[str, TrustedRepository] = field(default_factory=dict)
    items: dict[tuple[str, str], Item] = field(default_factory=dict)
    releases: dict[str, Release] = field(default_factory=dict)
    requirements: dict[str, list[Requirement]] = field(default_factory=dict)
    provisions: dict[str, list[Provision]] = field(default_factory=dict)
    deployments: dict[str, Deployment] = field(default_factory=dict)
    revisions: dict[tuple[str, int], Revision] = field(default_factory=dict)
    resolutions: dict[str, list[Resolution]] = field(default_factory=dict)
    aliases: dict[tuple[str, str | None, str], Alias] = field(default_factory=dict)
    approvals: dict[str, Approval] = field(default_factory=dict)
    evaluations: dict[str, Evaluation] = field(default_factory=dict)
    revocations: dict[str, Revocation] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    observations: dict[tuple[str, str, str, str], Observation] = field(default_factory=dict)

    def copy(self) -> _State:
        # Records are frozen, so copying the containers is enough.
        out = _State(meta=self.meta, seq=self.seq)
        for name in (
            "clusters",
            "tenants",
            "repositories",
            "items",
            "releases",
            "deployments",
            "revisions",
            "aliases",
            "approvals",
            "evaluations",
            "revocations",
            "observations",
        ):
            setattr(out, name, dict(getattr(self, name)))
        for name in ("requirements", "provisions", "resolutions"):
            setattr(out, name, {k: list(v) for k, v in getattr(self, name).items()})
        out.events = list(self.events)
        return out


class _Reads:
    _s: _State

    def meta(self) -> Meta:
        return self._s.meta

    def generation(self) -> int:
        return self._s.seq

    def clusters(self) -> list[Cluster]:
        return sorted(self._s.clusters.values(), key=lambda c: c.name)

    def cluster(self, name: str) -> Cluster | None:
        return self._s.clusters.get(name)

    def tenants(self, cluster: str | None = None) -> list[Tenant]:
        rows = [t for t in self._s.tenants.values() if cluster in (None, t.cluster)]
        return sorted(rows, key=lambda t: (t.cluster, t.name))

    def tenant(self, cluster: str, name: str) -> Tenant | None:
        return self._s.tenants.get((cluster, name))

    def trusted_repositories(self) -> list[TrustedRepository]:
        return sorted(self._s.repositories.values(), key=lambda r: r.repository)

    def items(self, kind: str | None = None) -> list[Item]:
        rows = [i for i in self._s.items.values() if kind in (None, i.kind)]
        return sorted(rows, key=lambda i: (i.kind, i.name))

    def item(self, kind: str, name: str) -> Item | None:
        return self._s.items.get((kind, name))

    def releases(self, kind: str | None = None, item: str | None = None) -> list[Release]:
        rows = [
            r
            for r in self._s.releases.values()
            if kind in (None, r.kind) and item in (None, r.item)
        ]
        return sorted(rows, key=lambda r: r.id)

    def release(self, release_id: str) -> Release | None:
        return self._s.releases.get(release_id)

    def requirements(
        self,
        release_id: str | None = None,
        *,
        type: str | None = None,
        target: str | None = None,
    ) -> list[Requirement]:
        rows = [
            r
            for key in sorted(self._s.requirements)
            if release_id in (None, key)
            for r in self._s.requirements[key]
            if type in (None, r.type) and target in (None, r.target)
        ]
        return rows

    def provisions(self, release_id: str | None = None) -> list[Provision]:
        return [
            p
            for key in sorted(self._s.provisions)
            if release_id in (None, key)
            for p in self._s.provisions[key]
        ]

    def deployments(
        self,
        *,
        kind: str | None = None,
        cluster: str | None = None,
        tenant: str | None = None,
        authority: str | None = None,
    ) -> list[Deployment]:
        rows = [
            d
            for d in self._s.deployments.values()
            if kind in (None, d.kind)
            and cluster in (None, d.cluster)
            and tenant in (None, d.tenant)
            and authority in (None, d.authority)
        ]
        return sorted(rows, key=lambda d: d.id)

    def deployment(self, deployment_id: str) -> Deployment | None:
        return self._s.deployments.get(deployment_id)

    def revisions(self, deployment_id: str) -> list[Revision]:
        rows = [r for (dep, _), r in self._s.revisions.items() if dep == deployment_id]
        return sorted(rows, key=lambda r: r.number)

    def revision(self, deployment_id: str, number: int) -> Revision | None:
        return self._s.revisions.get((deployment_id, number))

    def resolutions(self, revision_id: str) -> list[Resolution]:
        return list(self._s.resolutions.get(revision_id, []))

    def aliases(self, cluster: str | None = None) -> list[Alias]:
        rows = [a for a in self._s.aliases.values() if cluster in (None, a.cluster)]
        return sorted(rows, key=lambda a: (a.cluster, a.tenant or "", a.alias))

    def approvals(self, deployment_id: str | None = None) -> list[Approval]:
        rows = [a for a in self._s.approvals.values() if deployment_id in (None, a.deployment_id)]
        return sorted(rows, key=lambda a: (a.approved_at, a.id))

    def approval(self, approval_id: str) -> Approval | None:
        return self._s.approvals.get(approval_id)

    def evaluations(
        self, deployment_id: str | None = None, *, state: str | None = None
    ) -> list[Evaluation]:
        rows = [
            e
            for e in self._s.evaluations.values()
            if deployment_id in (None, e.deployment_id) and state in (None, e.state)
        ]
        return sorted(rows, key=lambda e: (e.created_at, e.id))

    def evaluation(self, evaluation_id: str) -> Evaluation | None:
        return self._s.evaluations.get(evaluation_id)

    def revocations(self, *, kind: str | None = None, item: str | None = None) -> list[Revocation]:
        rows = [
            r
            for r in self._s.revocations.values()
            if kind in (None, r.kind) and item in (None, r.item)
        ]
        return sorted(rows, key=lambda r: (r.revoked_at, r.id))

    def revocation(self, revocation_id: str) -> Revocation | None:
        return self._s.revocations.get(revocation_id)

    def events(
        self, *, deployment_id: str | None = None, after: int = 0, limit: int = 200
    ) -> list[Event]:
        rows = [
            e for e in self._s.events if e.seq > after and deployment_id in (None, e.deployment_id)
        ]
        return rows[:limit]

    def observations(
        self, cluster: str, *, subject_kind: str | None = None, namespace: str | None = None
    ) -> list[Observation]:
        rows = [
            o
            for o in self._s.observations.values()
            if o.cluster == cluster
            and subject_kind in (None, o.subject_kind)
            and namespace in (None, o.namespace)
        ]
        return sorted(rows, key=lambda o: (o.subject_kind, o.namespace, o.name))

    def observation(
        self, cluster: str, subject_kind: str, namespace: str, name: str
    ) -> Observation | None:
        return self._s.observations.get((cluster, subject_kind, namespace, name))


def _same_release(a: Release, b: Release) -> bool:
    return a.model_dump(exclude={"recorded_at"}) == b.model_dump(exclude={"recorded_at"})


_CONTENT = {"phase", "approval_id", "reason", "activated_at", "generation"}
_EVALUATION_PROGRESS = {
    "state",
    "claimed_by",
    "claim_expires_at",
    "evaluator_version",
    "result",
    "finished_at",
}


class _Tx(_Reads):
    def __init__(self, state: _State, actor: str, clock: Callable[[], datetime]):
        self._s = state
        self._actor = actor
        self._clock = clock
        self.changed = False
        self.evented = False

    def _put(self, table: dict, key: Any, record: Any) -> None:
        if table.get(key) != record:
            table[key] = record
            self.changed = True

    def set_mode(self, mode: Mode, reason: str) -> None:
        if self._s.meta.mode != mode or self._s.meta.mode_reason != reason:
            self._s.meta = Meta(mode=mode, mode_reason=reason, mode_changed_at=self._clock())
            self.changed = True

    def put_cluster(self, cluster: Cluster) -> None:
        self._put(self._s.clusters, cluster.name, cluster)

    def put_tenant(self, tenant: Tenant) -> None:
        self._put(self._s.tenants, (tenant.cluster, tenant.name), tenant)

    def put_trusted_repository(self, repository: TrustedRepository) -> None:
        self._put(self._s.repositories, repository.repository, repository)

    def put_item(self, item: Item) -> None:
        self._put(self._s.items, (item.kind, item.name), item)

    def put_alias(self, alias: Alias) -> None:
        self._put(self._s.aliases, (alias.cluster, alias.tenant, alias.alias), alias)

    def put_deployment(self, deployment: Deployment) -> None:
        if (deployment.kind, deployment.item) not in self._s.items:
            raise Missing(f"item {deployment.kind}/{deployment.item} does not exist")
        self._put(self._s.deployments, deployment.id, deployment)

    def add_release(self, release: Release) -> None:
        if (release.kind, release.item) not in self._s.items:
            raise Missing(f"item {release.kind}/{release.item} does not exist")
        have = self._s.releases.get(release.id)
        if have is None:
            self._s.releases[release.id] = release
            self.changed = True
        elif not _same_release(have, release):
            raise Conflict(f"release {release.id} exists with other content")

    def complete_release(self, release: Release) -> None:
        have = self._s.releases.get(release.id)
        if have is None:
            raise Missing(f"release {release.id} does not exist")
        if completion_is_new(have, release):
            self._s.releases[release.id] = release.model_copy(
                update={"recorded_at": have.recorded_at}
            )
            self.changed = True

    def _add_rows(self, table: dict[str, list], key: str, rows: list, what: str) -> None:
        have = table.get(key)
        if have is None:
            table[key] = list(rows)
            self.changed = True
        elif have != list(rows):
            raise Conflict(f"{what} of {key} exist with other content")

    def add_requirements(self, release_id: str, rows: list[Requirement]) -> None:
        if release_id not in self._s.releases:
            raise Missing(f"release {release_id} does not exist")
        if any(r.release_id != release_id for r in rows):
            raise Conflict("a requirement names another release")
        self._add_rows(self._s.requirements, release_id, rows, "requirements")

    def add_provisions(self, release_id: str, rows: list[Provision]) -> None:
        if release_id not in self._s.releases:
            raise Missing(f"release {release_id} does not exist")
        if any(r.release_id != release_id for r in rows):
            raise Conflict("a provision names another release")
        self._add_rows(self._s.provisions, release_id, rows, "provisions")

    def add_revision(self, revision: Revision) -> None:
        if revision.deployment_id not in self._s.deployments:
            raise Missing(f"deployment {revision.deployment_id} does not exist")
        if revision.release_id not in self._s.releases:
            raise Missing(f"release {revision.release_id} does not exist")
        key = (revision.deployment_id, revision.number)
        have = self._s.revisions.get(key)
        if have is not None:
            if have.model_dump(exclude=_CONTENT | {"created_at"}) != revision.model_dump(
                exclude=_CONTENT | {"created_at"}
            ):
                raise Conflict(f"revision {revision.id} exists with other content")
            return
        highest = max(
            (n for (dep, n) in self._s.revisions if dep == revision.deployment_id), default=0
        )
        if revision.number != highest + 1:
            raise Conflict(f"revision {revision.id} is out of sequence; next is {highest + 1}")
        self._s.revisions[key] = revision
        self.changed = True

    def add_resolutions(self, revision_id: str, rows: list[Resolution]) -> None:
        if any(r.revision_id != revision_id for r in rows):
            raise Conflict("a resolution names another revision")
        self._add_rows(self._s.resolutions, revision_id, rows, "resolutions")

    def set_revision_progress(
        self,
        deployment_id: str,
        number: int,
        *,
        phase: Phase | None = None,
        approval_id: str | None = None,
        reason: str | None = None,
        activated_at: datetime | None = None,
        generation: int | None = None,
    ) -> Revision:
        have = self._s.revisions.get((deployment_id, number))
        if have is None:
            raise Missing(f"revision {deployment_id}@{number} does not exist")
        if approval_id is not None and approval_id not in self._s.approvals:
            raise Missing(f"approval {approval_id} does not exist")
        given = {
            "phase": phase,
            "approval_id": approval_id,
            "reason": reason,
            "activated_at": activated_at,
            "generation": generation,
        }
        updated = have.model_copy(update={k: v for k, v in given.items() if v is not None})
        self._put(self._s.revisions, (deployment_id, number), updated)
        return updated

    def add_approval(self, approval: Approval) -> None:
        if approval.id in self._s.approvals:
            raise Conflict(f"approval {approval.id} exists")
        if (approval.deployment_id, approval.revision) not in self._s.revisions:
            raise Missing(f"revision {approval.deployment_id}@{approval.revision} does not exist")
        self._s.approvals[approval.id] = approval
        self.changed = True

    def set_approval_state(self, approval_id: str, state: ApprovalState) -> None:
        have = self._s.approvals.get(approval_id)
        if have is None:
            raise Missing(f"approval {approval_id} does not exist")
        self._put(self._s.approvals, approval_id, have.model_copy(update={"state": state}))

    def add_evaluation(self, evaluation: Evaluation) -> None:
        if (evaluation.deployment_id, evaluation.revision) not in self._s.revisions:
            raise Missing(
                f"revision {evaluation.deployment_id}@{evaluation.revision} does not exist"
            )
        have = self._s.evaluations.get(evaluation.id)
        if have is not None:
            skip = _EVALUATION_PROGRESS | {"created_at"}
            if have.model_dump(exclude=skip) != evaluation.model_dump(exclude=skip):
                raise Conflict(f"evaluation {evaluation.id} exists with other content")
            return
        highest = max(
            (
                e.attempt
                for e in self._s.evaluations.values()
                if (e.deployment_id, e.revision) == (evaluation.deployment_id, evaluation.revision)
            ),
            default=0,
        )
        if evaluation.attempt != highest + 1:
            raise Conflict(f"evaluation {evaluation.id} is out of sequence; next is {highest + 1}")
        self._s.evaluations[evaluation.id] = evaluation
        self.changed = True

    def set_evaluation_progress(
        self,
        evaluation_id: str,
        *,
        state: EvaluationState | None = None,
        claimed_by: str | None = None,
        claim_expires_at: datetime | None = None,
        evaluator_version: str | None = None,
        result: dict[str, Any] | None = None,
        finished_at: datetime | None = None,
    ) -> Evaluation:
        have = self._s.evaluations.get(evaluation_id)
        if have is None:
            raise Missing(f"evaluation {evaluation_id} does not exist")
        given = {
            "state": state,
            "claimed_by": claimed_by,
            "claim_expires_at": claim_expires_at,
            "evaluator_version": evaluator_version,
            "result": result,
            "finished_at": finished_at,
        }
        updated = have.model_copy(update={k: v for k, v in given.items() if v is not None})
        self._put(self._s.evaluations, evaluation_id, updated)
        return updated

    def add_revocation(self, revocation: Revocation) -> None:
        have = self._s.revocations.get(revocation.id)
        if have is None:
            self._s.revocations[revocation.id] = revocation
            self.changed = True
        elif have.model_dump(exclude={"indexed_at"}) != revocation.model_dump(
            exclude={"indexed_at"}
        ):
            raise Conflict(f"revocation {revocation.id} exists with other content")

    def event(
        self, type: str, *, deployment_id: str | None = None, detail: dict[str, Any] | None = None
    ) -> Event:
        self._s.seq += 1
        event = Event(
            seq=self._s.seq,
            at=self._clock(),
            actor=self._actor,
            type=type,
            deployment_id=deployment_id,
            detail=detail or {},
        )
        self._s.events.append(event)
        self.evented = True
        return event

    def advance_generation(self, at_least: int) -> None:
        if at_least > self._s.seq:
            self._s.seq = at_least
            self.changed = True


class MemoryStore(_Reads):
    def __init__(self, clock: Callable[[], datetime] = _now):
        self._s = _State()
        self._clock = clock
        self._lock = threading.RLock()
        self._open = False

    @contextmanager
    def transaction(self, actor: str) -> Iterator[_Tx]:
        with self._lock:
            if self._open:
                raise StoreError("a transaction is already open on this thread")
            self._open = True
            try:
                tx = _Tx(self._s.copy(), actor, self._clock)
                yield tx
                if tx.changed != tx.evented:
                    raise Unevented(
                        "a change needs an event" if tx.changed else "an event needs a change"
                    )
                self._s = tx._s
            finally:
                self._open = False

    def put_observations(
        self,
        cluster: str,
        observations: list[Observation],
        *,
        complete_for: list[tuple[str, str]] | None = None,
    ) -> None:
        with self._lock:
            table = dict(self._s.observations)
            seen = set()
            for o in observations:
                if o.cluster != cluster:
                    raise Conflict("an observation names another cluster")
                key = (cluster, o.subject_kind, o.namespace, o.name)
                table[key] = o
                seen.add(key)
            newest = max((o.observed_at for o in observations), default=self._clock())
            for key, have in list(table.items()):
                if (
                    key[0] == cluster
                    and (key[1], key[2]) in (complete_for or [])
                    and key not in seen
                    and have.present
                ):
                    table[key] = have.model_copy(update={"present": False, "observed_at": newest})
            self._s.observations = table

    def ping(self) -> None:
        return None
