"""The catalog of skills, from the agent releases that carry them (`contract/skills.md`
section 11.2).

A skill reaches the registry only inside an agent release. Whatever records an agent release
(the release report, the operator's route, the cluster import, recovery) calls `record_skills`
in the same transaction.
"""

from __future__ import annotations

import re
from typing import Any

from agent_registry.models import Item, Release, release_id
from agent_registry.store import Tx

DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")


def record_skills(tx: Tx, agent_release: Release) -> list[str]:
    """Write the items and releases for `spec.skills` of an agent release. Returns the ids of
    what was written or changed; a skill and digest already recorded add nothing."""
    skills = (agent_release.document.get("spec") or {}).get("skills") or []
    agent = tx.item("agent", agent_release.item)
    owner = agent.owner if agent else ""
    source = agent.source if agent else ""
    written: list[str] = []
    for entry in skills:
        name, digest = str(entry.get("name") or ""), str(entry.get("digest") or "")
        if not name or not DIGEST.match(digest):
            continue
        description = str(entry.get("description") or "")
        have = tx.item("skill", name)
        if have is None:
            tx.put_item(
                Item(kind="skill", name=name, owner=owner, source=source, description=description)
            )
            written.append(f"skill:{name}")
        elif have.description != description:
            tx.put_item(have.model_copy(update={"description": description}))
            written.append(f"skill:{name}")
        release = _release(name, digest, entry, agent_release)
        if tx.release(release.id) is None:
            tx.add_release(release)
            written.append(release.id)
    return written


def _release(name: str, digest: str, entry: dict[str, Any], agent_release: Release) -> Release:
    return Release(
        id=release_id("skill", name, digest.replace(":", "-")),
        kind="skill",
        item=name,
        name=digest.replace(":", "-"),
        version=entry.get("version"),
        digest=digest,
        origin="built",
        lineage=agent_release.lineage,
        document=entry,
        recorded_at=agent_release.recorded_at,
    )
