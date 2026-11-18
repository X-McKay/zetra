"""The store interface. Two implementations: `memory.MemoryStore` and `pg.PgStore`.

Rules both implementations keep, each covered by `tests/store_suite.py`:

1. Desired state changes only inside `transaction()`. Reads may happen anywhere.
2. A transaction that changes desired state records at least one event, and an event is never
   recorded without a change. Leaving the `with` block with one and not the other raises
   `Unevented` and nothing is written.
3. An exception inside the `with` block leaves the store exactly as it was.
4. Write transactions are serialised: two never interleave.
5. Releases, requirements, provisions, revisions (apart from progress), resolutions, approvals
   (apart from state), evaluations (apart from progress), revocations and events are written
   once. Writing the same content again is a no-op; writing different content under the same
   key raises `Conflict`.
6. Nothing is deleted. There is no delete operation.
7. Event sequence numbers only grow. `generation()` is the highest one recorded.
8. Observations are not desired state: `put_observations` needs no transaction and no event.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Protocol

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


class StoreError(Exception):
    pass


class Conflict(StoreError):
    """A write-once record exists with other content, or a number is out of sequence."""


class Missing(StoreError):
    """A write names a record that does not exist."""


class Unavailable(StoreError):
    """The store cannot be reached. The HTTP interface answers 503 `unavailable`."""


class Unevented(StoreError):
    """A transaction changed state without an event, or recorded an event without a change."""


class Reads(Protocol):
    def meta(self) -> Meta: ...
    def generation(self) -> int: ...
    def clusters(self) -> list[Cluster]: ...
    def cluster(self, name: str) -> Cluster | None: ...
    def tenants(self, cluster: str | None = None) -> list[Tenant]: ...
    def tenant(self, cluster: str, name: str) -> Tenant | None: ...
    def trusted_repositories(self) -> list[TrustedRepository]: ...
    def items(self, kind: str | None = None) -> list[Item]: ...
    def item(self, kind: str, name: str) -> Item | None: ...
    def releases(self, kind: str | None = None, item: str | None = None) -> list[Release]: ...
    def release(self, release_id: str) -> Release | None: ...
    def requirements(
        self,
        release_id: str | None = None,
        *,
        type: str | None = None,
        target: str | None = None,
    ) -> list[Requirement]: ...
    def provisions(self, release_id: str | None = None) -> list[Provision]: ...
    def deployments(
        self,
        *,
        kind: str | None = None,
        cluster: str | None = None,
        tenant: str | None = None,
        authority: str | None = None,
    ) -> list[Deployment]: ...
    def deployment(self, deployment_id: str) -> Deployment | None: ...
    def revisions(self, deployment_id: str) -> list[Revision]:
        """In ascending order of number."""
        ...

    def revision(self, deployment_id: str, number: int) -> Revision | None: ...
    def resolutions(self, revision_id: str) -> list[Resolution]: ...
    def aliases(self, cluster: str | None = None) -> list[Alias]: ...
    def approvals(self, deployment_id: str | None = None) -> list[Approval]:
        """In ascending order of approved_at, then id."""
        ...

    def approval(self, approval_id: str) -> Approval | None: ...
    def evaluations(
        self, deployment_id: str | None = None, *, state: str | None = None
    ) -> list[Evaluation]:
        """In ascending order of created_at, then id."""
        ...

    def evaluation(self, evaluation_id: str) -> Evaluation | None: ...
    def revocations(self, *, kind: str | None = None, item: str | None = None) -> list[Revocation]:
        """In ascending order of revoked_at, then id."""
        ...

    def revocation(self, revocation_id: str) -> Revocation | None: ...
    def events(
        self, *, deployment_id: str | None = None, after: int = 0, limit: int = 200
    ) -> list[Event]:
        """Events with seq greater than `after`, ascending, at most `limit`."""
        ...

    def observations(
        self, cluster: str, *, subject_kind: str | None = None, namespace: str | None = None
    ) -> list[Observation]: ...
    def observation(
        self, cluster: str, subject_kind: str, namespace: str, name: str
    ) -> Observation | None: ...


class Tx(Reads, Protocol):
    """A write transaction. Reads on it see its own writes."""

    def set_mode(self, mode: Mode, reason: str) -> None: ...
    def put_cluster(self, cluster: Cluster) -> None: ...
    def put_tenant(self, tenant: Tenant) -> None: ...
    def put_trusted_repository(self, repository: TrustedRepository) -> None: ...
    def put_item(self, item: Item) -> None: ...
    def put_alias(self, alias: Alias) -> None: ...
    def put_deployment(self, deployment: Deployment) -> None:
        """Insert or replace. The caller sends the whole record."""
        ...

    def add_release(self, release: Release) -> None:
        """Write once. `recorded_at` is not compared when deciding whether content is the same."""
        ...

    def add_requirements(self, release_id: str, rows: list[Requirement]) -> None:
        """Write once per release: the full set of rows."""
        ...

    def add_provisions(self, release_id: str, rows: list[Provision]) -> None: ...
    def add_revision(self, revision: Revision) -> None:
        """Write once. `number` must be one more than the deployment's highest (1 for the
        first). The record's progress fields are stored as given."""
        ...

    def add_resolutions(self, revision_id: str, rows: list[Resolution]) -> None: ...
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
        """Change only the fields given (None leaves a field as it is)."""
        ...

    def add_approval(self, approval: Approval) -> None:
        """Write once. A second approval with the same id raises `Conflict`, even when equal."""
        ...

    def set_approval_state(self, approval_id: str, state: ApprovalState) -> None: ...
    def add_evaluation(self, evaluation: Evaluation) -> None:
        """Write once. `attempt` must be one more than the revision's highest (1 for the
        first). The record's progress fields are stored as given."""
        ...

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
        """Change only the fields given (None leaves a field as it is). Raises `Missing` when
        there is no such evaluation."""
        ...

    def add_revocation(self, revocation: Revocation) -> None:
        """Write once. `indexed_at` is not compared when deciding whether content is the same."""
        ...

    def event(
        self, type: str, *, deployment_id: str | None = None, detail: dict[str, Any] | None = None
    ) -> Event:
        """Record an event and return it with its sequence number."""
        ...

    def advance_generation(self, at_least: int) -> None:
        """Make the next event's sequence number greater than `at_least`. Used when leaving
        recovery, so that what the registry writes next is newer than anything on a cluster."""
        ...


class Store(Reads, Protocol):
    def transaction(self, actor: str) -> AbstractContextManager[Tx]: ...
    def put_observations(
        self,
        cluster: str,
        observations: list[Observation],
        *,
        complete_for: list[tuple[str, str]] | None = None,
    ) -> None:
        """Replace the last observation of each subject given.

        `complete_for` lists (subject_kind, namespace) pairs for which `observations` is the
        whole truth: a stored subject of that pair that is not in `observations` is kept and
        marked `present=False` with the newest `observed_at` given.
        """
        ...

    def ping(self) -> None:
        """Raise if the store cannot be reached."""
        ...
