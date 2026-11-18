"""Migrations against a real database. Needs `REGISTRY_TEST_DSN`."""

from __future__ import annotations

import threading

import psycopg
import pytest

from agent_registry import cli
from agent_registry.store import pg as pg_module
from agent_registry.store.pg import migrate, migrations_dir
from tests.test_store_pg import BASE_DSN, isolated_schema

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not BASE_DSN, reason="REGISTRY_TEST_DSN is not set"),
]


@pytest.fixture
def empty():
    with isolated_schema() as dsn:
        yield dsn


def recorded(dsn: str) -> list[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
    return [r[0] for r in rows]


def tables(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall()
    return {r[0] for r in rows}


def test_apply_to_an_empty_database_and_again(empty):
    names = [p.name for p in sorted(migrations_dir().glob("*.sql"))]
    assert names
    assert migrate(empty) == names
    after = tables(empty)
    assert {"meta", "events", "deployments", "schema_migrations"} <= after
    assert migrate(empty) == []
    assert tables(empty) == after
    assert recorded(empty) == names


def test_two_processes_starting_together_apply_each_file_once(empty, tmp_path, monkeypatch):
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE a (x int);")
    (tmp_path / "0002_b.sql").write_text("SELECT pg_sleep(0.3); CREATE TABLE b (x int);")
    (tmp_path / "0003_c.sql").write_text("CREATE TABLE c (x int);")
    monkeypatch.setattr(pg_module, "migrations_dir", lambda: tmp_path)
    results: list[list[str]] = []
    errors: list[BaseException] = []
    start = threading.Barrier(3)

    def run() -> None:
        try:
            start.wait(10)
            results.append(migrate(empty))
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors
    applied = sorted(name for names in results for name in names)
    assert applied == ["0001_a.sql", "0002_b.sql", "0003_c.sql"]
    assert recorded(empty) == applied


def test_a_failing_file_is_rolled_back_and_the_earlier_ones_stay(empty, tmp_path, monkeypatch):
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE a (x int);")
    (tmp_path / "0002_b.sql").write_text("CREATE TABLE b (x int); SELECT * FROM nowhere;")
    monkeypatch.setattr(pg_module, "migrations_dir", lambda: tmp_path)
    with pytest.raises(psycopg.errors.UndefinedTable):
        migrate(empty)
    assert recorded(empty) == ["0001_a.sql"]
    assert "b" not in tables(empty)
    (tmp_path / "0002_b.sql").write_text("CREATE TABLE b (x int);")
    assert migrate(empty) == ["0002_b.sql"]


def test_the_migrate_command_is_repeatable(empty, monkeypatch, capsys):
    monkeypatch.setenv("REGISTRY_DSN", empty)
    assert cli.main(["migrate"]) == 0
    assert cli.main(["migrate"]) == 0
    first, second = capsys.readouterr().out.strip().splitlines()
    assert '"0001_init.sql"' in first
    assert second == '{"applied": []}'
