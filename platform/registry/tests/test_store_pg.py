"""The PostgreSQL store: the shared suite, and what only a real database can show.

Needs `REGISTRY_TEST_DSN`; every test gets a schema of its own, created and dropped here.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_registry.store import Unavailable, Unevented
from agent_registry.store import pg as pg_module
from agent_registry.store.pg import PgStore, migrate
from tests.store_suite import DEP, StoreSuite, item, seed

BASE_DSN = os.environ.get("REGISTRY_TEST_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not BASE_DSN, reason="REGISTRY_TEST_DSN is not set"),
]


@contextmanager
def isolated_schema() -> Iterator[str]:
    """A DSN whose search_path is a new, empty schema; the schema is dropped afterwards."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(BASE_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            yield make_conninfo(BASE_DSN, options=f"-csearch_path={name}")
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture
def dsn() -> Iterator[str]:
    with isolated_schema() as value:
        migrate(value)
        yield value


class TestPgStore(StoreSuite):
    @pytest.fixture
    def store(self, dsn: str) -> Iterator[PgStore]:
        store = PgStore(dsn)
        yield store
        store.close()


def test_a_failure_between_the_change_and_the_event_leaves_neither(dsn, monkeypatch):
    store = PgStore(dsn)
    seed(store)
    before = store.generation()

    def broken(self, event):
        raise psycopg.errors.DataError("the event does not fit")

    monkeypatch.setattr(pg_module._Tx, "_insert_event", broken)
    with pytest.raises(psycopg.errors.DataError), store.transaction("test") as tx:
        tx.put_item(item("other"))
        tx.event("other-added")
    monkeypatch.undo()

    fresh = PgStore(dsn)
    try:
        assert fresh.item("agent", "other") is None
        assert fresh.generation() == before
        assert [e.type for e in fresh.events()] == ["seeded"]
    finally:
        fresh.close()
        store.close()


def test_a_lost_connection_between_the_change_and_the_event_is_unavailable(dsn, monkeypatch):
    store = PgStore(dsn)
    seed(store)

    def lost(self, event):
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(pg_module._Tx, "_insert_event", lost)
    with pytest.raises(Unavailable), store.transaction("test") as tx:
        tx.put_item(item("other"))
        tx.event("other-added")
    monkeypatch.undo()
    try:
        assert store.item("agent", "other") is None
        assert store.generation() == 1
    finally:
        store.close()


def test_a_rolled_back_transaction_uses_no_sequence_number(dsn):
    store = PgStore(dsn)
    try:
        seed(store)
        with pytest.raises(RuntimeError), store.transaction("test") as tx:
            tx.put_item(item("other"))
            assert tx.event("doomed").seq == 2
            raise RuntimeError("stop")
        with pytest.raises(Unevented), store.transaction("test") as tx:
            tx.put_item(item("other"))  # a change, and no event
        assert store.generation() == 1
        with store.transaction("test") as tx:
            tx.put_item(item("other"))
            assert tx.event("real").seq == 2
        assert [e.seq for e in store.events()] == [1, 2]
    finally:
        store.close()


def test_two_replicas_write_at_once_without_gaps(dsn):
    replicas = [PgStore(dsn), PgStore(dsn)]
    threads_per_replica, writes = 4, 6
    errors: list[BaseException] = []
    start = threading.Barrier(2 * threads_per_replica)

    def write(store: PgStore, worker: int) -> None:
        try:
            start.wait(10)
            for n in range(writes):
                with store.transaction(f"w{worker}") as tx:
                    tx.put_item(item(f"item-{worker}-{n}"))
                    tx.event("added", detail={"worker": worker, "n": n})
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [
        threading.Thread(target=write, args=(replicas[w % 2], w))
        for w in range(2 * threads_per_replica)
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not errors
        total = 2 * threads_per_replica * writes
        events = replicas[0].events(limit=total + 10)
        assert [e.seq for e in events] == list(range(1, total + 1))
        assert replicas[1].generation() == total
        assert len(replicas[1].items()) == total
    finally:
        for replica in replicas:
            replica.close()


def test_data_survives_a_new_store(dsn):
    first = PgStore(dsn)
    seed(first)
    first.close()
    second = PgStore(dsn)
    try:
        assert second.deployment(DEP) is not None
        assert second.generation() == 1
        assert second.events()[0].at.utcoffset().total_seconds() == 0
    finally:
        second.close()


def test_an_unreachable_database_is_unavailable(dsn):
    unreachable = "postgresql://registry@127.0.0.1:1/registry?connect_timeout=1"
    store = PgStore(unreachable)
    try:
        calls = [
            store.ping,
            store.clusters,
            lambda: store.put_observations("kubani", []),
            lambda: store.transaction("test").__enter__(),
        ]
        for call in calls:
            started = time.monotonic()
            with pytest.raises(Unavailable) as raised:
                call()
            assert time.monotonic() - started < 5
            assert unreachable not in str(raised.value)
            assert not isinstance(raised.value, psycopg.Error)
    finally:
        store.close()


def test_a_closed_store_is_unavailable(dsn):
    store = PgStore(dsn)
    store.close()
    with pytest.raises(Unavailable):
        store.ping()
