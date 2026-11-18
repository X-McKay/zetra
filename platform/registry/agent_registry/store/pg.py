"""The PostgreSQL store (package 1.1)."""

from __future__ import annotations

from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def migrations_dir() -> Path:
    """The numbered SQL files: beside the package in a checkout, inside it when installed."""
    packaged = Path(__file__).resolve().parents[1] / "_migrations"
    return packaged if packaged.is_dir() else MIGRATIONS


def migrate(dsn: str) -> list[str]:
    """Apply every migration not yet applied, in name order, each in its own transaction.
    Returns the names applied; [] when the database was up to date."""
    raise NotImplementedError


class PgStore:
    """`agent_registry.store.Store` on PostgreSQL. Passes `tests/store_suite.py`."""

    def __init__(self, dsn: str):
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
