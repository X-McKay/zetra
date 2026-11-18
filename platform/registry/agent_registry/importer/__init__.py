"""The importer (package 1.2): the catalog feed, and the agents of the pull request path.

Rules: `contract/import.md`. Both functions are idempotent: a second call with the same input
changes nothing and records no event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_registry.store import Store


@dataclass
class ImportReport:
    agents: int = 0  # Agent documents read
    releases: int = 0  # AgentRelease documents read
    created: list[str] = field(default_factory=list)  # ids of records written for the first time
    updated: list[str] = field(default_factory=list)  # ids of items or deployments that changed
    orphans: list[str] = field(default_factory=list)  # "<namespace>/<name>": release, no Agent
    skipped: list[str] = field(default_factory=list)  # deployment ids the registry owns
    problems: list[str] = field(default_factory=list)  # documents that could not be imported

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated)


def load_directory(path: Path) -> list[dict[str, Any]]:
    """Every `Agent` and `AgentRelease` document in the YAML files under `path`. Other kinds
    (kustomizations, RBAC) are ignored."""
    raise NotImplementedError


def load_feed(path: Path) -> dict[str, Any]:
    raise NotImplementedError


def apply_feed(
    store: Store, feed: dict[str, Any], *, now: datetime | None = None, actor: str = "feed"
) -> bool:
    """Write the feed's clusters, tenants, trusted repositories, tool servers, model engines
    and aliases. Returns True when anything changed."""
    raise NotImplementedError


def import_documents(
    store: Store,
    documents: list[dict[str, Any]],
    *,
    cluster: str,
    now: datetime,
    actor: str = "importer",
) -> ImportReport:
    """Index agents owned by the pull request path from their documents."""
    raise NotImplementedError


def index_observations(
    store: Store, cluster: str, *, now: datetime, actor: str = "indexer"
) -> ImportReport:
    """The same, with the documents taken from the cluster's last observations, so that the
    index follows what pull requests change. Called after each observation report."""
    raise NotImplementedError
