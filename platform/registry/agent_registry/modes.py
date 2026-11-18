"""Entering recovery. Leaving it is a rule with conditions: `rules/recovery.py`."""

from __future__ import annotations

from agent_registry.store import Store


def enter_recovery(store: Store, reason: str, *, actor: str) -> bool:
    """Put the registry in recovery. Returns False when it already was, for the same reason.

    In recovery nothing is projected and every write but leave-recovery is refused.
    """
    meta = store.meta()
    if meta.mode == "recovery" and meta.mode_reason == reason:
        return False
    with store.transaction(actor) as tx:
        tx.set_mode("recovery", reason)
        tx.event("recovery-entered", detail={"reason": reason})
    return True
