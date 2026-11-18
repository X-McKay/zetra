"""What the registry does with an observation report (`contract/projection.md` section 6).

A sequence, not a transaction: storing observations needs none, entering recovery and each
step of the importer and the lifecycle open their own."""

from __future__ import annotations

from datetime import datetime

from agent_registry import importer, modes
from agent_registry.models import Observation
from agent_registry.rules import alias_move, lifecycle
from agent_registry.store import Store


def recovery_reason(cluster: str, seen: int, registry: int) -> str:
    return f"cluster {cluster} has seen generation {seen}; the registry is at {registry}"


def accept(
    store: Store,
    cluster: str,
    *,
    observations: list[Observation],
    complete_for: list[tuple[str, str]],
    objects: int,
    observed_at: datetime,
    last_generation: int,
    holds: list[tuple[str, str, int]],
    now: datetime,
    max_age_seconds: int,
    actor: str,
) -> tuple[str, int, int]:
    """Store a report. Returns the mode, the generation and the number of observations stored.

    `observations` are the report's objects, subjects and projection results; the `report`
    observation is added here.
    """
    at = min(observed_at, now)
    stored = [o.model_copy(update={"observed_at": at}) for o in observations]
    stored.append(
        Observation(
            cluster=cluster,
            subject_kind="projection",
            namespace="",
            name="report",
            observed_at=at,
            document={"lastGeneration": last_generation, "objects": objects},
        )
    )
    store.put_observations(cluster, stored, complete_for=complete_for)

    registry = store.generation()
    if last_generation > registry:
        modes.enter_recovery(
            store, recovery_reason(cluster, last_generation, registry), actor=actor
        )

    if store.meta().mode == "normal":
        importer.index_observations(store, cluster, now=now)
        alias_move.acknowledge(store, cluster, holds, now=now, actor=actor)
        lifecycle.advance_cluster(store, cluster, now=now, max_age_seconds=max_age_seconds)
        alias_move.advance(store, cluster, now=now)
    return store.meta().mode, store.generation(), len(stored)
