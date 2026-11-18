"""Effects a release requests beyond its tools, and whether its tenant grants them.

The rules are section 7 of `platform/registry/contract/skills.md`. One function decides
whether a request is granted, and every program that needs the answer calls it: the Registry
Controller for a release's Ready condition, and the loop test's played controller.
"""

from __future__ import annotations

from typing import Any

from agent_platform.constants import LABEL_PREFIX

ANNOTATION_GRANTED_EFFECTS = f"{LABEL_PREFIX}granted-effects"

# What an approval that covers the effect shows. Held equal to
# `platform/registry/contract/effects.json` by a test.
STATEMENTS = {
    "sandbox_execute": (
        "skill scripts run inside the agent's container and can read the session's identity token"
    ),
}
KNOWN = tuple(sorted(STATEMENTS))


def requested(agent_spec: dict[str, Any] | None) -> list[str]:
    """The effects an Agent spec requests (`capabilities.effects`), sorted, none twice."""
    capabilities = (agent_spec or {}).get("capabilities")
    effects = capabilities.get("effects") if isinstance(capabilities, dict) else None
    if not isinstance(effects, list):
        return []
    return sorted({e for e in effects if isinstance(e, str)})


def granted(namespace: dict[str, Any] | None) -> set[str]:
    """The effects a tenant Namespace grants: the names in its annotation, split at commas.
    No Namespace, no annotation or an empty one grants nothing."""
    metadata = (namespace or {}).get("metadata")
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    value = annotations.get(ANNOTATION_GRANTED_EFFECTS) if isinstance(annotations, dict) else None
    if not isinstance(value, str):
        return set()
    return {name.strip() for name in value.split(",") if name.strip()}


def ungranted(agent_spec: dict[str, Any] | None, namespace: dict[str, Any] | None) -> list[str]:
    """The requested effects the Namespace does not grant, sorted. [] means the release may
    be Ready as far as effects go. An effect this code does not know is never granted,
    whatever the annotation says."""
    allowed = granted(namespace) & set(KNOWN)
    return [effect for effect in requested(agent_spec) if effect not in allowed]


def statement(effect: str) -> str:
    """The line an approval shows for an effect it covers."""
    return f"effect {effect}: {STATEMENTS[effect]}"
