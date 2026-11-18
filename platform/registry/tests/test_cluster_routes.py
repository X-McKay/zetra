"""The observation report: `contract/projection.md` section 6, through the HTTP application."""

from __future__ import annotations

import json
from datetime import timedelta

from agent_registry import modes
from agent_registry.importer import apply_feed
from agent_registry.models import Observation
from tests.test_write_cases import record
from tests.world import (
    CLUSTER,
    DEP,
    FIXTURES,
    NAMESPACE,
    T0,
    World,
    fixture_documents,
    registry_owned,
)

OBSERVATIONS = f"/v1/clusters/{CLUSTER}/observations"
EXAMPLE = FIXTURES.parent.parent / "contract" / "examples" / "observations.json"


def post(world: World, body: dict):
    return world.post(OBSERVATIONS, body, token="provisioner")


def empty(world: World, **fields) -> dict:
    return {"observedAt": world.now.isoformat(), "lastGeneration": 0} | fields


def as_object(observation: Observation) -> dict:
    return {
        "subjectKind": observation.subject_kind,
        "namespace": observation.namespace,
        "name": observation.name,
        "inFluxInventory": observation.in_flux_inventory,
        "document": observation.document,
    }


def pad_to(world: World, generation: int) -> None:
    with world.store.transaction("test") as tx:
        tx.advance_generation(generation - 1)
        tx.event("test-padding")


def counts(world: World) -> tuple:
    return (world.store.generation(), len(world.store.events(limit=10000)))


def test_the_example_report_is_accepted_for_the_seeded_world():
    world = World()
    world.seed()
    pad_to(world, 3)
    body = json.loads(EXAMPLE.read_text())
    world.now = T0 + timedelta(seconds=30)

    answer = post(world, body)

    assert answer.status_code == 200, answer.text
    sent = sum(len(body[key]) for key in ("objects", "subjects", "projections"))
    assert answer.json()["mode"] == "normal"
    assert answer.json()["stored"] == sent + 1
    stored = world.store.observations(CLUSTER)
    assert {(o.subject_kind, o.name) for o in stored} >= {
        ("agent", "db-admin"),
        ("projection", "report"),
    }
    assert len(stored) == sent + 1


def test_a_report_with_an_unknown_inventory_is_stored_as_unknown():
    world = World()
    world.seed()
    world.cluster_shows_everything()
    objects = [as_object(o) | {"inFluxInventory": None} for o in world.store.observations(CLUSTER)]

    answer = post(world, empty(world, objects=objects))

    assert answer.status_code == 200, answer.text
    stored = world.store.observations(CLUSTER, subject_kind="agent")
    assert [o.in_flux_inventory for o in stored] == [None]
    assert {o.in_flux_inventory for o in world.store.observations(CLUSTER)} == {None}


def test_the_report_and_the_projection_results_are_stored():
    world = registry_owned()
    agent = world.store.observation(CLUSTER, "agent", NAMESPACE, "db-admin")
    body = empty(
        world,
        lastGeneration=2,
        objects=[as_object(agent)],
        projections=[
            {
                "deployment": DEP,
                "kind": "AgentRelease",
                "namespace": NAMESPACE,
                "name": "db-admin-x",
                "generation": 2,
                "result": "refused-flux-inventory",
                "message": "in Flux",
            }
        ],
    )

    answer = post(world, body)

    assert answer.json() == {"mode": "normal", "generation": 2, "stored": 3}
    result = world.store.observation(CLUSTER, "projection", NAMESPACE, "agentrelease/db-admin-x")
    assert result.document == {
        "deployment": DEP,
        "generation": 2,
        "result": "refused-flux-inventory",
        "message": "in Flux",
    }
    heard = world.store.observation(CLUSTER, "projection", "", "report")
    assert heard.document == {"lastGeneration": 2, "objects": 1}
    assert heard.observed_at == world.now


def test_the_time_stored_is_the_earlier_of_the_reported_and_the_registrys():
    world = World()
    world.seed()
    later = world.now + timedelta(hours=1)
    earlier = world.now - timedelta(minutes=5)

    post(world, empty(world) | {"observedAt": later.isoformat()})
    assert world.store.observation(CLUSTER, "projection", "", "report").observed_at == world.now
    post(world, empty(world) | {"observedAt": earlier.isoformat()})
    assert world.store.observation(CLUSTER, "projection", "", "report").observed_at == earlier


def test_a_subject_and_a_complete_listing_mark_what_is_gone():
    world = World()
    world.seed()
    world.cluster_shows_everything()
    complete = [{"subjectKind": "agentrelease", "namespace": NAMESPACE}]
    subject = {
        "subjectKind": "tool-server",
        "namespace": "mcp",
        "name": "github-mcp",
        "present": False,
        "document": {},
    }

    answer = post(world, empty(world, complete=complete, subjects=[subject]))

    assert answer.status_code == 200, answer.text
    assert [o.present for o in world.store.observations(CLUSTER, subject_kind="agentrelease")] == [
        False,
        False,
    ]
    assert world.store.observation(CLUSTER, "agent", NAMESPACE, "db-admin").present is True
    assert world.store.observation(CLUSTER, "tool-server", "mcp", "github-mcp").present is False


def test_agents_of_the_pull_request_path_are_indexed():
    world = World()
    apply_feed(world.store, json.loads((FIXTURES / "feed.json").read_text()), now=world.now)
    agent, releases = fixture_documents()
    objects = [
        {
            "subjectKind": "agent",
            "namespace": NAMESPACE,
            "name": "db-admin",
            "inFluxInventory": True,
            "document": {"metadata": agent["metadata"], "spec": agent["spec"], "status": {}},
        },
        *[
            {
                "subjectKind": "agentrelease",
                "namespace": NAMESPACE,
                "name": r["metadata"]["name"],
                "inFluxInventory": True,
                "document": {"metadata": r["metadata"], "spec": r["spec"], "status": {}},
            }
            for r in releases
        ],
    ]

    answer = post(world, empty(world, objects=objects))

    assert answer.status_code == 200, answer.text
    deployment = world.store.deployment(DEP)
    assert deployment is not None and deployment.authority == "pull-request"
    assert len(world.store.revisions(DEP)) == 2
    assert [e.type for e in world.store.events()].count("agent-imported") == 1


def test_registry_owned_agents_are_advanced():
    world = registry_owned()
    number = record(world, "a")
    world.tick()
    observed = world.cluster_shows(number)
    assert world.store.revision(DEP, number).phase == "awaiting-runtime"

    answer = post(world, empty(world, lastGeneration=0, objects=[as_object(observed)]))

    assert answer.status_code == 200, answer.text
    revision = world.store.revision(DEP, number)
    assert revision.phase == "pending-approval"
    assert [e.type for e in world.store.events(deployment_id=DEP)][-1] == "approval-needed"


def test_a_last_generation_above_the_registrys_puts_the_registry_in_recovery():
    world = registry_owned()
    above = world.store.generation() + 5

    answer = post(world, empty(world, lastGeneration=above))

    assert answer.status_code == 200, answer.text
    assert answer.json()["mode"] == "recovery"
    assert world.store.meta().mode == "recovery"
    assert f"cluster {CLUSTER} has seen generation {above}" in world.store.meta().mode_reason
    assert world.store.observation(CLUSTER, "projection", "", "report") is not None


def test_a_report_in_recovery_is_stored_and_nothing_is_indexed_or_advanced():
    world = registry_owned()
    number = record(world, "a")
    modes.enter_recovery(world.store, "a test", actor="test")
    world.tick()
    observed = world.cluster_shows(number)
    before = counts(world)

    answer = post(world, empty(world, objects=[as_object(observed)]))

    assert answer.status_code == 200, answer.text
    assert answer.json() == {"mode": "recovery", "generation": before[0], "stored": 2}
    assert counts(world) == before
    assert world.store.revision(DEP, number).phase == "awaiting-runtime"
    assert world.store.observation(CLUSTER, "projection", "", "report") is not None


def test_a_report_for_another_clusters_provisioner_is_refused():
    world = World()

    answer = world.post("/v1/clusters/elsewhere/observations", empty(world), token="provisioner")

    assert answer.status_code == 403
