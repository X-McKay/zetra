"""What an import did."""

from __future__ import annotations

from dataclasses import dataclass, field


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
