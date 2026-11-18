"""The importer (package 1.2): the catalog feed, and the agents of the pull request path.

Rules: `contract/import.md`. Both functions are idempotent: a second call with the same input
changes nothing and records no event.
"""

from __future__ import annotations

from agent_registry.importer.documents import (
    import_documents,
    index_observations,
    load_directory,
)
from agent_registry.importer.feed import apply_feed, load_feed
from agent_registry.importer.report import ImportReport

__all__ = [
    "ImportReport",
    "apply_feed",
    "import_documents",
    "index_observations",
    "load_directory",
    "load_feed",
]
