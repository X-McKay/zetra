"""Which releases are revoked (`contract/delivery.md` section 8).

`revoked` is the one function other rules call; its signature is fixed. Import this module,
not its names, from `rules.revisions`: the two modules call each other.
"""

from __future__ import annotations

from agent_registry.models import Release, Revocation
from agent_registry.store import Reads


def revoked(reads: Reads, release: Release) -> Revocation | None:
    """The revocation of this release, or None. The oldest, when there are several."""
    for revocation in reads.revocations(kind=release.kind, item=release.item):
        if revocation.release == release.name:
            return revocation
    return None
