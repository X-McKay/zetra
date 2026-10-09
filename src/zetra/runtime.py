"""Typed cooperative runtime controls and an atomic local receiver example.

Python code can bypass these helpers. Production isolation, independent approval
identity, revocation/fencing and remote receiver idempotency must be external.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class Denied(PermissionError):
    """A cooperative runtime check denied the operation."""


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def action_digest(value: dict) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Agent(Protocol):
    def run(self, request: dict) -> dict: ...


class MCPReader(Protocol):
    def call(
        self, tool: str, arguments: dict, *, release_id: str, run_id: str, operation_id: str
    ) -> dict: ...


@dataclass
class RuntimeDependencies:
    capabilities: frozenset[str]
    max_steps: int
    max_seconds: int
    max_cost_usd: float
    stop_file: Path
    knowledge: dict[str, str] = field(default_factory=dict)
    action_store: ActionStore | None = None
    gateway_client: MCPReader | None = None
    release_id: str = "local-development"
    run_id: str = "local-run"
    _started: float = field(default_factory=time.monotonic)
    _steps: int = 0
    _cost: float = 0

    def require(self, capability: str, cost_usd: float = 0) -> None:
        if self.stop_file.exists():
            raise Denied("Agent stopped by local cooperative stop file")
        if time.monotonic() - self._started >= self.max_seconds:
            raise Denied("Time budget exhausted")
        if capability not in self.capabilities:
            raise Denied(f"Capability denied: {capability}")
        if (
            isinstance(cost_usd, bool)
            or not isinstance(cost_usd, (int, float))
            or not math.isfinite(cost_usd)
            or cost_usd < 0
        ):
            raise Denied("Cost reservation must be a nonnegative finite number")
        if self._steps >= self.max_steps or self._cost + cost_usd > self.max_cost_usd:
            raise Denied("Step or reserved-cost budget exhausted")
        self._steps += 1
        self._cost += cost_usd

    def read_knowledge(self, key: str) -> str:
        self.require("knowledge.read")
        return self.knowledge.get(key, "No approved knowledge entry found.")

    def create_ticket(
        self, payload: dict, *, actor: str, approval: str, idempotency_key: str
    ) -> dict:
        self.require("ticket.write")
        if self.action_store is None:
            raise Denied("No write receiver is configured")
        return self.action_store.commit_ticket(
            payload, actor=actor, approval=approval, idempotency_key=idempotency_key
        )

    def read_status(self) -> dict:
        self.require("mcp.read_status")
        if self.gateway_client is None:
            raise Denied("No qualified MCP gateway client is configured")
        return self.gateway_client.call(
            "read_status",
            {},
            release_id=self.release_id,
            run_id=self.run_id,
            operation_id=f"{self.run_id}:status-{self._steps}",
        )


class ActionStore:
    """SQLite demonstration receiver: approval consumption, effect and receipt
    share one database transaction. Not a production approval service or signer.
    The authenticated approval service must call issue(), never the agent.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS grants (
                    token TEXT PRIMARY KEY, actor TEXT NOT NULL, digest TEXT NOT NULL,
                    expires REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS receipts (
                    operation_key TEXT PRIMARY KEY, actor TEXT NOT NULL, digest TEXT NOT NULL,
                    grant_token TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL);
            """)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    def issue(self, payload: dict, *, actor: str, ttl_seconds: int = 300) -> str:
        self.validate_payload(payload)
        if (
            not actor
            or not isinstance(actor, str)
            or type(ttl_seconds) is not int
            or not 0 < ttl_seconds <= 3600
        ):
            raise Denied("Grant requires actor and lifetime 1..3600 seconds")
        token = secrets.token_urlsafe(32)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO grants(token,actor,digest,expires) VALUES(?,?,?,?)",
                (token, actor, action_digest(payload), time.time() + ttl_seconds),
            )
        return token

    @staticmethod
    def validate_payload(payload: dict) -> None:
        if not isinstance(payload, dict) or set(payload) != {"title", "body"}:
            raise Denied("Ticket payload requires exactly title and body")
        if any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in payload.values()):
            raise Denied("Ticket title/body must be nonempty strings <= 4000 characters")

    def commit_ticket(
        self, payload: dict, *, actor: str, approval: str, idempotency_key: str
    ) -> dict:
        self.validate_payload(payload)
        if not isinstance(actor, str) or not actor or not isinstance(approval, str) or not approval:
            raise Denied("Authenticated actor and approval are required")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise Denied("Idempotency key is required and <= 128 characters")
        digest = action_digest(payload)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            receipt = conn.execute(
                "SELECT actor,digest,grant_token,result FROM receipts WHERE operation_key=?",
                (idempotency_key,),
            ).fetchone()
            if receipt:
                if receipt[:3] != (actor, digest, approval):
                    raise Denied(
                        "Idempotency key was used for a different actor, payload or approval"
                    )
                return json.loads(receipt[3])
            grant = conn.execute(
                "SELECT actor,digest,expires,consumed FROM grants WHERE token=?", (approval,)
            ).fetchone()
            if (
                not grant
                or grant[0] != actor
                or grant[1] != digest
                or grant[2] <= time.time()
                or grant[3]
            ):
                raise Denied("Approval missing, mismatched, expired or consumed")
            conn.execute("UPDATE grants SET consumed=1 WHERE token=?", (approval,))
            cursor = conn.execute(
                "INSERT INTO tickets(title,body) VALUES(?,?)", (payload["title"], payload["body"])
            )
            result = {"ticketId": cursor.lastrowid, "status": "created"}
            conn.execute(
                "INSERT INTO receipts VALUES(?,?,?,?,?)",
                (idempotency_key, actor, digest, approval, canonical(result)),
            )
            return result
