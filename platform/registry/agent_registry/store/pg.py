"""The PostgreSQL store (package 1.1)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

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
from agent_registry.store import (
    Conflict,
    Missing,
    StoreError,
    Unavailable,
    Unevented,
    completion_is_new,
)

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

# Held while migrations are applied, so two processes starting together do not both apply one.
_MIGRATION_LOCK = 0x72656731


def migrations_dir() -> Path:
    """The numbered SQL files: beside the package in a checkout, inside it when installed."""
    packaged = Path(__file__).resolve().parents[1] / "_migrations"
    return packaged if packaged.is_dir() else MIGRATIONS


def _now() -> datetime:
    return datetime.now(UTC)


def _translate(error: psycopg.Error) -> StoreError | None:
    """The store error for a database error, or None when it should propagate as it is."""
    if isinstance(error, psycopg.OperationalError | psycopg.InterfaceError):
        # The first line names the reason (refused, timed out, closed) and never the DSN.
        reason = (str(error).strip().splitlines() or [type(error).__name__])[0]
        return Unavailable(f"the database is unavailable: {reason}")
    if isinstance(error, psycopg.errors.UniqueViolation):
        return Conflict(str(error).splitlines()[0])
    if isinstance(error, psycopg.errors.ForeignKeyViolation):
        return Missing(str(error).splitlines()[0])
    return None


@contextmanager
def _translated() -> Iterator[None]:
    try:
        yield
    except psycopg.Error as error:
        mapped = _translate(error)
        if mapped is None:
            raise
        raise mapped from error


def migrate(dsn: str) -> list[str]:
    """Apply every migration not yet applied, in name order, each in its own transaction.
    Returns the names applied; [] when the database was up to date."""
    files = sorted(migrations_dir().glob("*.sql"), key=lambda p: p.name)
    applied: list[str] = []
    with _translated(), psycopg.connect(dsn) as conn:
        # Every step takes the lock and looks again: another process may have got in between
        # this one's steps.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.commit()
        for path in files:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
            done = conn.execute("SELECT 1 FROM schema_migrations WHERE name = %s", (path.name,))
            if done.fetchone() is None:
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                applied.append(path.name)
            conn.commit()
    return applied


# -- records to rows and back -------------------------------------------------------------


def _dump(record: Any) -> dict[str, Any]:
    """Column values of a record; documents become jsonb."""
    out = {}
    for name in type(record).model_fields:
        value = getattr(record, name)
        out[name] = Jsonb(value) if isinstance(value, dict | list) else value
    if isinstance(record, Alias):
        out["tenant"] = record.tenant or ""  # '' is a cluster-wide alias, so the key has no null
    return out


def _load(model: type, row: dict[str, Any]) -> Any:
    for name, value in row.items():
        if isinstance(value, datetime):
            row[name] = value.astimezone(UTC)
    if model is Alias and row["tenant"] == "":
        row["tenant"] = None
    return model.model_validate(row)


def _columns(model: type) -> str:
    return ", ".join(model.model_fields)


def _where(filters: dict[str, Any]) -> tuple[str, list[Any]]:
    given = {k: v for k, v in filters.items() if v is not None}
    clause = " AND ".join(f"{k} = %s" for k in given)
    return (f" WHERE {clause}" if clause else ""), list(given.values())


# ORDER BY clauses. Text sorts by code point, as the memory store's Python sort does, not by
# the database's collation.
_C = 'COLLATE "C"'
_BY_NAME = f"name {_C}"
_BY_KIND_NAME = f"kind {_C}, name {_C}"
_BY_ID = f"id {_C}"


class _Reads:
    """Every read, over whatever connection `_rows` runs on."""

    def _rows(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _select(self, model: type, table: str, order: str, **filters: Any) -> list[Any]:
        where, params = _where(filters)
        sql = f"SELECT {_columns(model)} FROM {table}{where} ORDER BY {order}"
        return [_load(model, row) for row in self._rows(sql, params)]

    def _one(self, model: type, table: str, **key: Any) -> Any:
        where, params = _where(key)
        rows = self._rows(f"SELECT {_columns(model)} FROM {table}{where}", params)
        return _load(model, rows[0]) if rows else None

    def meta(self) -> Meta:
        rows = self._rows("SELECT mode, mode_reason, mode_changed_at FROM meta")
        return _load(Meta, rows[0])

    def generation(self) -> int:
        return self._rows("SELECT seq FROM meta")[0]["seq"]

    def clusters(self) -> list[Cluster]:
        return self._select(Cluster, "clusters", _BY_NAME)

    def cluster(self, name: str) -> Cluster | None:
        return self._one(Cluster, "clusters", name=name)

    def tenants(self, cluster: str | None = None) -> list[Tenant]:
        return self._select(Tenant, "tenants", f"cluster {_C}, name {_C}", cluster=cluster)

    def tenant(self, cluster: str, name: str) -> Tenant | None:
        return self._one(Tenant, "tenants", cluster=cluster, name=name)

    def trusted_repositories(self) -> list[TrustedRepository]:
        return self._select(TrustedRepository, "trusted_repositories", f"repository {_C}")

    def items(self, kind: str | None = None) -> list[Item]:
        return self._select(Item, "items", _BY_KIND_NAME, kind=kind)

    def item(self, kind: str, name: str) -> Item | None:
        return self._one(Item, "items", kind=kind, name=name)

    def releases(self, kind: str | None = None, item: str | None = None) -> list[Release]:
        return self._select(Release, "releases", _BY_ID, kind=kind, item=item)

    def release(self, release_id: str) -> Release | None:
        return self._one(Release, "releases", id=release_id)

    def requirements(
        self,
        release_id: str | None = None,
        *,
        type: str | None = None,
        target: str | None = None,
    ) -> list[Requirement]:
        return self._select(
            Requirement,
            "release_requirements",
            f"release_id {_C}, position",
            release_id=release_id,
            type=type,
            target=target,
        )

    def provisions(self, release_id: str | None = None) -> list[Provision]:
        return self._select(
            Provision, "release_provisions", f"release_id {_C}, position", release_id=release_id
        )

    def deployments(
        self,
        *,
        kind: str | None = None,
        cluster: str | None = None,
        tenant: str | None = None,
        authority: str | None = None,
    ) -> list[Deployment]:
        return self._select(
            Deployment,
            "deployments",
            _BY_ID,
            kind=kind,
            cluster=cluster,
            tenant=tenant,
            authority=authority,
        )

    def deployment(self, deployment_id: str) -> Deployment | None:
        return self._one(Deployment, "deployments", id=deployment_id)

    def revisions(self, deployment_id: str) -> list[Revision]:
        return self._select(Revision, "deployment_revisions", "number", deployment_id=deployment_id)

    def revision(self, deployment_id: str, number: int) -> Revision | None:
        return self._one(
            Revision, "deployment_revisions", deployment_id=deployment_id, number=number
        )

    def resolutions(self, revision_id: str) -> list[Resolution]:
        return self._select(Resolution, "revision_resolutions", "position", revision_id=revision_id)

    def aliases(self, cluster: str | None = None) -> list[Alias]:
        return self._select(
            Alias, "aliases", f"cluster {_C}, tenant {_C}, alias {_C}", cluster=cluster
        )

    def approvals(self, deployment_id: str | None = None) -> list[Approval]:
        return self._select(
            Approval, "approvals", f"approved_at, id {_C}", deployment_id=deployment_id
        )

    def approval(self, approval_id: str) -> Approval | None:
        return self._one(Approval, "approvals", id=approval_id)

    def evaluations(
        self, deployment_id: str | None = None, *, state: str | None = None
    ) -> list[Evaluation]:
        return self._select(
            Evaluation,
            "evaluations",
            f"created_at, id {_C}",
            deployment_id=deployment_id,
            state=state,
        )

    def evaluation(self, evaluation_id: str) -> Evaluation | None:
        return self._one(Evaluation, "evaluations", id=evaluation_id)

    def revocations(self, *, kind: str | None = None, item: str | None = None) -> list[Revocation]:
        return self._select(Revocation, "revocations", f"revoked_at, id {_C}", kind=kind, item=item)

    def revocation(self, revocation_id: str) -> Revocation | None:
        return self._one(Revocation, "revocations", id=revocation_id)

    def events(
        self, *, deployment_id: str | None = None, after: int = 0, limit: int = 200
    ) -> list[Event]:
        where, params = _where({"deployment_id": deployment_id})
        where += (" AND" if where else " WHERE") + " seq > %s"
        sql = f"SELECT {_columns(Event)} FROM events{where} ORDER BY seq LIMIT %s"
        return [_load(Event, row) for row in self._rows(sql, [*params, after, limit])]

    def observations(
        self, cluster: str, *, subject_kind: str | None = None, namespace: str | None = None
    ) -> list[Observation]:
        return self._select(
            Observation,
            "observations",
            f"subject_kind {_C}, namespace {_C}, name {_C}",
            cluster=cluster,
            subject_kind=subject_kind,
            namespace=namespace,
        )

    def observation(
        self, cluster: str, subject_kind: str, namespace: str, name: str
    ) -> Observation | None:
        return self._one(
            Observation,
            "observations",
            cluster=cluster,
            subject_kind=subject_kind,
            namespace=namespace,
            name=name,
        )


def _same_release(a: Release, b: Release) -> bool:
    return a.model_dump(exclude={"recorded_at"}) == b.model_dump(exclude={"recorded_at"})


_PROGRESS = {"phase", "approval_id", "reason", "activated_at", "generation"}
_EVALUATION_PROGRESS = {
    "state",
    "claimed_by",
    "claim_expires_at",
    "evaluator_version",
    "result",
    "finished_at",
}


class _Tx(_Reads):
    """A write transaction: one database transaction that holds the `meta` row lock."""

    def __init__(self, conn: psycopg.Connection, actor: str, clock: Callable[[], datetime]):
        self._conn = conn
        self._actor = actor
        self._clock = clock
        self.changed = False
        self.evented = False

    def _rows(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return self._conn.execute(sql, params).fetchall()

    def _insert(self, table: str, record: Any, **extra: Any) -> None:
        values = extra | _dump(record)
        marks = ", ".join(["%s"] * len(values))
        self._conn.execute(
            f"INSERT INTO {table} ({', '.join(values)}) VALUES ({marks})", list(values.values())
        )

    def _put(self, table: str, key: tuple[str, ...], record: Any) -> None:
        """Insert or replace; a record equal to the stored one is not a change."""
        values = _dump(record)
        if self._one(type(record), table, **{k: values[k] for k in key}) == record:
            return
        update = ", ".join(f"{c} = EXCLUDED.{c}" for c in values if c not in key)
        marks = ", ".join(["%s"] * len(values))
        self._conn.execute(
            f"INSERT INTO {table} ({', '.join(values)}) VALUES ({marks})"
            f" ON CONFLICT ({', '.join(key)}) DO UPDATE SET {update}",
            list(values.values()),
        )
        self.changed = True

    def set_mode(self, mode: Mode, reason: str) -> None:
        have = self.meta()
        if have.mode != mode or have.mode_reason != reason:
            self._conn.execute(
                "UPDATE meta SET mode = %s, mode_reason = %s, mode_changed_at = %s",
                (mode, reason, self._clock()),
            )
            self.changed = True

    def put_cluster(self, cluster: Cluster) -> None:
        self._put("clusters", ("name",), cluster)

    def put_tenant(self, tenant: Tenant) -> None:
        self._put("tenants", ("cluster", "name"), tenant)

    def put_trusted_repository(self, repository: TrustedRepository) -> None:
        self._put("trusted_repositories", ("repository",), repository)

    def put_item(self, item: Item) -> None:
        self._put("items", ("kind", "name"), item)

    def put_alias(self, alias: Alias) -> None:
        self._put("aliases", ("cluster", "tenant", "alias"), alias)

    def put_deployment(self, deployment: Deployment) -> None:
        if self.item(deployment.kind, deployment.item) is None:
            raise Missing(f"item {deployment.kind}/{deployment.item} does not exist")
        self._put("deployments", ("id",), deployment)

    def add_release(self, release: Release) -> None:
        if self.item(release.kind, release.item) is None:
            raise Missing(f"item {release.kind}/{release.item} does not exist")
        have = self.release(release.id)
        if have is None:
            self._insert("releases", release)
            self.changed = True
        elif not _same_release(have, release):
            raise Conflict(f"release {release.id} exists with other content")

    def complete_release(self, release: Release) -> None:
        have = self.release(release.id)
        if have is None:
            raise Missing(f"release {release.id} does not exist")
        if completion_is_new(have, release):
            self._conn.execute(
                "UPDATE releases SET digest = %s, document = %s WHERE id = %s",
                (release.digest, Jsonb(release.document), release.id),
            )
            self.changed = True

    def _add_rows(
        self, table: str, what: str, release_id: str, rows: list[Any], have: list[Any]
    ) -> None:
        """Write once per release, even when the set is empty (release_described says so)."""
        described = self._rows(
            "SELECT 1 FROM release_described WHERE release_id = %s AND what = %s",
            (release_id, what),
        )
        if described:
            if have != list(rows):
                raise Conflict(f"{what} of {release_id} exist with other content")
            return
        self._conn.execute(
            "INSERT INTO release_described (release_id, what) VALUES (%s, %s)", (release_id, what)
        )
        for position, row in enumerate(rows):
            self._insert(table, row, position=position)
        self.changed = True

    def add_requirements(self, release_id: str, rows: list[Requirement]) -> None:
        if self.release(release_id) is None:
            raise Missing(f"release {release_id} does not exist")
        if any(r.release_id != release_id for r in rows):
            raise Conflict("a requirement names another release")
        have = self.requirements(release_id)
        self._add_rows("release_requirements", "requirements", release_id, rows, have)

    def add_provisions(self, release_id: str, rows: list[Provision]) -> None:
        if self.release(release_id) is None:
            raise Missing(f"release {release_id} does not exist")
        if any(r.release_id != release_id for r in rows):
            raise Conflict("a provision names another release")
        have = self.provisions(release_id)
        self._add_rows("release_provisions", "provisions", release_id, rows, have)

    def add_revision(self, revision: Revision) -> None:
        if self.deployment(revision.deployment_id) is None:
            raise Missing(f"deployment {revision.deployment_id} does not exist")
        if self.release(revision.release_id) is None:
            raise Missing(f"release {revision.release_id} does not exist")
        have = self.revision(revision.deployment_id, revision.number)
        if have is not None:
            skip = _PROGRESS | {"created_at"}
            if have.model_dump(exclude=skip) != revision.model_dump(exclude=skip):
                raise Conflict(f"revision {revision.id} exists with other content")
            return
        highest = self._rows(
            "SELECT coalesce(max(number), 0) AS n FROM deployment_revisions"
            " WHERE deployment_id = %s",
            (revision.deployment_id,),
        )[0]["n"]
        if revision.number != highest + 1:
            raise Conflict(f"revision {revision.id} is out of sequence; next is {highest + 1}")
        self._insert("deployment_revisions", revision)
        self.changed = True

    def add_resolutions(self, revision_id: str, rows: list[Resolution]) -> None:
        if any(r.revision_id != revision_id for r in rows):
            raise Conflict("a resolution names another revision")
        described = self._rows(
            "SELECT 1 FROM revision_resolved WHERE revision_id = %s", (revision_id,)
        )
        if described:
            if self.resolutions(revision_id) != list(rows):
                raise Conflict(f"resolutions of {revision_id} exist with other content")
            return
        self._conn.execute(
            "INSERT INTO revision_resolved (revision_id) VALUES (%s)", (revision_id,)
        )
        for position, row in enumerate(rows):
            self._insert("revision_resolutions", row, position=position)
        self.changed = True

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
        have = self.revision(deployment_id, number)
        if have is None:
            raise Missing(f"revision {deployment_id}@{number} does not exist")
        if approval_id is not None and self.approval(approval_id) is None:
            raise Missing(f"approval {approval_id} does not exist")
        given = {
            "phase": phase,
            "approval_id": approval_id,
            "reason": reason,
            "activated_at": activated_at,
            "generation": generation,
        }
        given = {k: v for k, v in given.items() if v is not None}
        updated = have.model_copy(update=given)
        if updated != have:
            sets = ", ".join(f"{k} = %s" for k in given)
            self._conn.execute(
                f"UPDATE deployment_revisions SET {sets} WHERE deployment_id = %s AND number = %s",
                [*given.values(), deployment_id, number],
            )
            self.changed = True
        return updated

    def add_approval(self, approval: Approval) -> None:
        if self.approval(approval.id) is not None:
            raise Conflict(f"approval {approval.id} exists")
        if self.revision(approval.deployment_id, approval.revision) is None:
            raise Missing(f"revision {approval.deployment_id}@{approval.revision} does not exist")
        self._insert("approvals", approval)
        self.changed = True

    def set_approval_state(self, approval_id: str, state: ApprovalState) -> None:
        have = self.approval(approval_id)
        if have is None:
            raise Missing(f"approval {approval_id} does not exist")
        if have.state != state:
            self._conn.execute(
                "UPDATE approvals SET state = %s WHERE id = %s", (state, approval_id)
            )
            self.changed = True

    def add_evaluation(self, evaluation: Evaluation) -> None:
        if self.revision(evaluation.deployment_id, evaluation.revision) is None:
            raise Missing(
                f"revision {evaluation.deployment_id}@{evaluation.revision} does not exist"
            )
        have = self.evaluation(evaluation.id)
        if have is not None:
            skip = _EVALUATION_PROGRESS | {"created_at"}
            if have.model_dump(exclude=skip) != evaluation.model_dump(exclude=skip):
                raise Conflict(f"evaluation {evaluation.id} exists with other content")
            return
        highest = self._rows(
            "SELECT coalesce(max(attempt), 0) AS n FROM evaluations"
            " WHERE deployment_id = %s AND revision = %s",
            (evaluation.deployment_id, evaluation.revision),
        )[0]["n"]
        if evaluation.attempt != highest + 1:
            raise Conflict(f"evaluation {evaluation.id} is out of sequence; next is {highest + 1}")
        self._insert("evaluations", evaluation)
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
        have = self.evaluation(evaluation_id)
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
        given = {k: v for k, v in given.items() if v is not None}
        updated = have.model_copy(update=given)
        if updated != have:
            sets = ", ".join(f"{k} = %s" for k in given)
            values = [Jsonb(v) if isinstance(v, dict) else v for v in given.values()]
            self._conn.execute(
                f"UPDATE evaluations SET {sets} WHERE id = %s", [*values, evaluation_id]
            )
            self.changed = True
        return updated

    def add_revocation(self, revocation: Revocation) -> None:
        have = self.revocation(revocation.id)
        if have is None:
            self._insert("revocations", revocation)
            self.changed = True
        elif have.model_dump(exclude={"indexed_at"}) != revocation.model_dump(
            exclude={"indexed_at"}
        ):
            raise Conflict(f"revocation {revocation.id} exists with other content")

    def _insert_event(self, event: Event) -> None:
        self._insert("events", event)

    def event(
        self, type: str, *, deployment_id: str | None = None, detail: dict[str, Any] | None = None
    ) -> Event:
        seq = self._rows("UPDATE meta SET seq = seq + 1 RETURNING seq")[0]["seq"]
        event = Event(
            seq=seq,
            at=self._clock(),
            actor=self._actor,
            type=type,
            deployment_id=deployment_id,
            detail=detail or {},
        )
        self._insert_event(event)
        self.evented = True
        return event

    def advance_generation(self, at_least: int) -> None:
        moved = self._conn.execute("UPDATE meta SET seq = %s WHERE seq < %s", (at_least, at_least))
        if moved.rowcount:
            self.changed = True


class PgStore(_Reads):
    """`agent_registry.store.Store` on PostgreSQL. Passes `tests/store_suite.py`.

    Safe to share between threads, and between processes on the same database. A write
    transaction holds one pooled connection for its whole length, so a thread that also reads
    through the store from inside it needs a second one: keep `max_size` above the number of
    threads that write at once.
    """

    def __init__(
        self,
        dsn: str,
        *,
        clock: Callable[[], datetime] = _now,
        max_size: int = 10,
        timeout: float | None = None,
    ):
        self._clock = clock
        self._local = threading.local()
        if timeout is None:
            # Waiting for a connection includes making one, so follow the DSN's own limit.
            try:
                connect = float(conninfo_to_dict(dsn).get("connect_timeout") or 0)
            except (psycopg.ProgrammingError, ValueError):
                connect = 0
            timeout = connect + 1 if connect else 10
        self._pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=max_size,
            timeout=timeout,
            kwargs={"row_factory": dict_row},
            configure=self._configure,
            check=ConnectionPool.check_connection,
            open=False,
        )
        self._pool.open()  # does not wait: an unreachable database shows on first use

    @staticmethod
    def _configure(conn: psycopg.Connection) -> None:
        conn.execute("SET TIME ZONE 'UTC'")
        conn.commit()

    def close(self) -> None:
        self._pool.close()

    def _rows(self, sql: str, params: list[Any] | tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with _translated(), self._pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    @contextmanager
    def transaction(self, actor: str) -> Iterator[_Tx]:
        if getattr(self._local, "open", False):
            raise StoreError("a transaction is already open on this thread")
        self._local.open = True
        try:
            with _translated(), self._pool.connection() as conn:
                conn.execute("SELECT 1 FROM meta FOR UPDATE")
                tx = _Tx(conn, actor, self._clock)
                yield tx
                if tx.changed != tx.evented:
                    raise Unevented(
                        "a change needs an event" if tx.changed else "an event needs a change"
                    )
        finally:
            self._local.open = False

    def put_observations(
        self,
        cluster: str,
        observations: list[Observation],
        *,
        complete_for: list[tuple[str, str]] | None = None,
    ) -> None:
        if any(o.cluster != cluster for o in observations):
            raise Conflict("an observation names another cluster")
        given = {(o.subject_kind, o.namespace, o.name): o for o in observations}
        newest = max((o.observed_at for o in observations), default=None) or self._clock()
        key = ("cluster", "subject_kind", "namespace", "name")
        with _translated(), self._pool.connection() as conn:
            # Two replicas reporting for one cluster take turns, so rows are never locked in
            # opposite orders.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"observations:{cluster}",))
            for _, o in sorted(given.items()):
                values = _dump(o)
                update = ", ".join(f"{c} = EXCLUDED.{c}" for c in values if c not in key)
                marks = ", ".join(["%s"] * len(values))
                conn.execute(
                    f"INSERT INTO observations ({', '.join(values)}) VALUES ({marks})"
                    f" ON CONFLICT ({', '.join(key)}) DO UPDATE SET {update}",
                    list(values.values()),
                )
            for subject_kind, namespace in complete_for or []:
                names = [n for (k, ns, n) in given if (k, ns) == (subject_kind, namespace)]
                conn.execute(
                    "UPDATE observations SET present = false, observed_at = %s"
                    " WHERE cluster = %s AND subject_kind = %s AND namespace = %s"
                    " AND present AND name <> ALL(%s::text[])",
                    (newest, cluster, subject_kind, namespace, names),
                )

    def ping(self) -> None:
        self._rows("SELECT 1")
