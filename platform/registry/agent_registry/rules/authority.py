"""Whether one authority asks for more than another.

An approval covers a later release only when the later release asks for no more than what
was approved. "No more" has to be provable from the two documents: anything that cannot be
shown to be the same or less counts as more.

Both arguments are in the shape `canonical.authority_of` returns. Cases:
`contract/authority-cases.json`.
"""

from __future__ import annotations

from typing import Any

# What an approval of the effect covers (`contract/effects.json`; a test holds the two equal).
STATEMENTS = {
    "sandbox_execute": (
        "skill scripts run inside the agent's container and can read the session's identity token"
    ),
}


def _at_most(candidate: Any, base: Any) -> bool:
    """A limit is no higher. No limit at all (None) is only matched by no limit."""
    if candidate is None or base is None:
        return candidate is None and base is None
    if isinstance(candidate, bool) or isinstance(base, bool):
        return False
    if not isinstance(candidate, int | float) or not isinstance(base, int | float):
        return False
    return candidate <= base


def widens(base: dict[str, Any], candidate: dict[str, Any]) -> list[str]:
    """Every way `candidate` asks for more than `base`, in plain words. [] when it does not.

    Asking for less (a tool dropped, a lower budget, a shorter lifetime) is not a change that
    needs approval.
    """
    if not base:
        return ["there is no approved authority to compare with"]
    reasons: list[str] = []

    was, now = base.get("identity") or {}, candidate.get("identity") or {}
    for key in ("mode", "onBehalfOf"):
        if was.get(key) != now.get(key):
            reasons.append(f"identity {key} changes from {was.get(key)!r} to {now.get(key)!r}")

    approved_models = {m.get("backend"): m for m in base.get("models") or []}
    for entry in candidate.get("models") or []:
        backend = entry.get("backend")
        approved = approved_models.get(backend)
        if approved is None:
            reasons.append(f"model backend {backend} is new")
            continue
        added = sorted(set(entry.get("models") or []) - set(approved.get("models") or []))
        if added:
            reasons.append(f"model backend {backend} adds models {', '.join(added)}")
        for limit in ("tokensPerDay", "tokensPerSession"):
            before = (approved.get("budget") or {}).get(limit)
            after = (entry.get("budget") or {}).get(limit)
            if not _at_most(after, before):
                reasons.append(
                    f"model backend {backend} raises {limit} from {before!r} to {after!r}"
                )

    approved_tools = {t.get("backend"): t for t in base.get("mcp") or []}
    for entry in candidate.get("mcp") or []:
        backend = entry.get("backend")
        approved = approved_tools.get(backend)
        if approved is None:
            reasons.append(f"tool backend {backend} is new")
            continue
        for key in ("effect", "delegation"):
            if approved.get(key) != entry.get(key):
                reasons.append(
                    f"tool backend {backend} changes {key} "
                    f"from {approved.get(key)!r} to {entry.get(key)!r}"
                )
        added = sorted(set(entry.get("tools") or []) - set(approved.get("tools") or []))
        if added:
            reasons.append(f"tool backend {backend} adds tools {', '.join(added)}")

    was, now = base.get("runtime") or {}, candidate.get("runtime") or {}
    for limit in ("ttlSeconds", "maxBoundaries"):
        if not _at_most(now.get(limit), was.get(limit)):
            reasons.append(f"runtime {limit} rises from {was.get(limit)!r} to {now.get(limit)!r}")
    for key in ("runtimeClass", "cpu", "memory"):
        if was.get(key) != now.get(key):
            reasons.append(f"runtime {key} changes from {was.get(key)!r} to {now.get(key)!r}")

    for effect in sorted(set(candidate.get("effects") or []) - set(base.get("effects") or [])):
        reasons.append(
            f"effect {effect}: {STATEMENTS.get(effect, 'an effect the registry does not know')}"
        )
    return reasons
