"""Receivers commit writes. The agent process proposes; a receiver is the authority.

Layer 0 semantics: a grant names one operation, one payload digest and one actor; the
receiver recomputes the digest from the payload it is about to commit, refuses a
mismatch, and returns the original receipt when the same operation is delivered twice.
Grants are unsigned at this layer; signed grants with receiver-held keys are layer 2.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from .contracts import Contract, digest
from .errors import Denied


class Grant(Contract):
    operation_id: str
    digest: str
    actor: str
    mode: Literal["human", "policy"]
    expires_at: datetime | None = None


class Receipt(Contract):
    operation_id: str
    digest: str
    receiver: str
    receipt_id: str
    committed_at: datetime
    status: Literal["committed"] = "committed"


class Receiver(Protocol):
    name: str

    def commit(self, grant: Grant, payload: Contract) -> Receipt: ...
    def describe(self) -> dict[str, object]: ...


def payload_digest(payload: Contract) -> str:
    return digest(payload.model_dump(mode="json"))


def _check(grant: Grant, payload: Contract) -> str:
    actual = payload_digest(payload)
    if actual != grant.digest:
        raise Denied("payload differs from the approved payload")
    if grant.expires_at is not None and grant.expires_at <= datetime.now(UTC):
        raise Denied("grant has expired")
    return actual


class MemoryReceiver:
    """In-process ledger used by tests and `zetra run`. Same rules as the SQLite receiver."""

    def __init__(self, name: str):
        self.name = name
        self.receipts: dict[str, tuple[str, str, Receipt]] = {}
        self.commits: list[Contract] = []

    def commit(self, grant: Grant, payload: Contract) -> Receipt:
        actual = _check(grant, payload)
        previous = self.receipts.get(grant.operation_id)
        if previous is not None:
            if previous[:2] != (actual, grant.actor):
                raise Denied("operation id was already used for a different payload or actor")
            return previous[2]
        receipt = Receipt(
            operation_id=grant.operation_id,
            digest=actual,
            receiver=self.name,
            receipt_id=secrets.token_hex(8),
            committed_at=datetime.now(UTC),
        )
        self.receipts[grant.operation_id] = (actual, grant.actor, receipt)
        self.commits.append(payload)
        return receipt

    def describe(self) -> dict[str, object]:
        return {"name": self.name, "idempotent": True, "kind": "memory"}


class SqliteReceiver:
    """Durable local ledger: consumption, effect and receipt share one transaction."""

    def __init__(self, name: str, path: Path):
        self.name, self.path = name, path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS receipts (
                    operation_id TEXT PRIMARY KEY, digest TEXT NOT NULL, actor TEXT NOT NULL,
                    payload TEXT NOT NULL, receipt TEXT NOT NULL);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def commit(self, grant: Grant, payload: Contract) -> Receipt:
        actual = _check(grant, payload)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT digest, actor, receipt FROM receipts WHERE operation_id=?",
                (grant.operation_id,),
            ).fetchone()
            if row:
                if (row[0], row[1]) != (actual, grant.actor):
                    raise Denied("operation id was already used for a different payload or actor")
                return Receipt.model_validate_json(row[2])
            receipt = Receipt(
                operation_id=grant.operation_id,
                digest=actual,
                receiver=self.name,
                receipt_id=secrets.token_hex(8),
                committed_at=datetime.now(UTC),
            )
            conn.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?)",
                (
                    grant.operation_id,
                    actual,
                    grant.actor,
                    json.dumps(payload.model_dump(mode="json"), sort_keys=True),
                    receipt.model_dump_json(),
                ),
            )
            return receipt

    def describe(self) -> dict[str, object]:
        return {"name": self.name, "idempotent": True, "kind": "sqlite", "path": str(self.path)}

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT count(*) FROM receipts").fetchone()[0])
