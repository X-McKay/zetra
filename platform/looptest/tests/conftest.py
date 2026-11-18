"""Worlds for the loop tests, once on the memory store and once on PostgreSQL.

The PostgreSQL run needs `REGISTRY_TEST_DSN`; each world gets a schema of its own, created
and dropped here.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_registry.store.memory import MemoryStore
from agent_registry.store.pg import PgStore, migrate
from loopworld.world import World

BASE_DSN = os.environ.get("REGISTRY_TEST_DSN")


@contextmanager
def _pg_store(clock) -> Iterator[PgStore]:
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(BASE_DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            dsn = make_conninfo(BASE_DSN, options=f"-csearch_path={name}")
            migrate(dsn)
            store = PgStore(dsn, clock=clock)
            try:
                yield store
            finally:
                store.close()
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture(
    params=[
        "memory",
        pytest.param(
            "postgres",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(not BASE_DSN, reason="REGISTRY_TEST_DSN is not set"),
            ],
        ),
    ]
)
def new_world(request) -> Iterator[Callable[[], World]]:
    """A function that makes a new world: a registry with an empty store, and a cluster with
    the platform's own objects on it."""
    with ExitStack() as stack:

        def make_store(clock):
            if request.param == "memory":
                return MemoryStore(clock=clock)
            return stack.enter_context(_pg_store(clock))

        yield lambda: World(make_store)


@pytest.fixture
def world(new_world) -> World:
    return new_world()
