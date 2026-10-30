"""Section 13.4 runner records: one JSON line per event, audit metadata only (13.9)."""

from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger("runner.records")

FIELDS = {
    "session_started": {"tenant", "agent", "release", "session_id", "delegated", "grant_handle"},
    "sandbox_created": {"session_id", "generation", "pod", "node", "boot_ms"},
    "boundary_accepted": {
        "session_id",
        "generation",
        "boundary",
        "kind",
        "tool",
        "input_tokens",
        "output_tokens",
        "cumulative_tokens",
        "snapshot",
        "summary_sha256",
    },
    "sandbox_replaced": {"session_id", "old_generation", "reason"},
    "session_finished": {"session_id", "outcome", "boundaries", "cumulative_tokens", "duration_ms"},
    "cleanup_result": {"session_id", "generation", "result", "source", "pod"},
    "cleanup_reconciled": {"tenant", "orphans_deleted", "orphans_unconfirmed", "sessions_closed"},
    "orphan_persisting": {"tenant", "pod", "session_id", "generation", "deleting_since"},
}


def summary_hash(summary: str) -> str:
    """The boundary summary is task content; records carry only its hash."""
    return "sha256:" + hashlib.sha256(summary.encode("utf-8")).hexdigest()


def emit(event: str, **fields) -> dict:
    allowed = FIELDS[event]
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"{event} does not record {sorted(unknown)}")
    record = {"event": event, **fields}
    logger.info(json.dumps(record, sort_keys=True, separators=(",", ":")))
    return record
