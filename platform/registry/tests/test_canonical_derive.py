"""`canonical` and `derive`: the functions the importer and the rules share."""

from __future__ import annotations

import copy
from datetime import timedelta

from agent_registry import canonical, derive
from agent_registry.config import ANNOTATION_PREFIX
from tests.world import AGENT, CLUSTER, DEP, NAMESPACE, T0, TENANT, World, fixture_documents


def test_a_digest_does_not_depend_on_key_order():
    assert canonical.digest({"a": 1, "b": [1, 2]}) == canonical.digest({"b": [1, 2], "a": 1})
    assert canonical.digest({"a": 1}) != canonical.digest({"a": 2})
    assert len(canonical.digest({})) == 64


def test_authority_is_what_an_agent_may_do_and_nothing_else():
    agent, _ = fixture_documents()
    same = copy.deepcopy(agent)
    same["spec"]["owner"] = "someone else"
    same["spec"]["description"] = "other words"
    same["spec"]["capabilities"]["mcp"][0]["tools"] = ["execute_sql"][::-1]
    more = copy.deepcopy(agent)
    more["spec"]["capabilities"]["mcp"][0]["tools"].append("list_tables")

    authority = canonical.authority_of(agent)

    assert canonical.authority_of(same) == authority
    assert canonical.authority_digest(canonical.authority_of(more)) != canonical.authority_digest(
        authority
    )
    assert authority["identity"] == {"mode": "agent", "onBehalfOf": None}
    assert authority["runtime"]["ttlSeconds"] == 900
    assert canonical.authority_of({}) != canonical.EMPTY_AUTHORITY


def test_the_kept_documents_carry_no_approval_and_no_other_metadata():
    agent, releases = fixture_documents()
    release = copy.deepcopy(releases[0])
    release["metadata"]["annotations"] = {"x": "y"}
    agent = copy.deepcopy(agent)
    agent["metadata"]["labels"] = {"x": "y"}
    agent["status"] = {"ready": True}

    kept = canonical.release_document(release)
    kept_agent = canonical.agent_document(agent)

    assert "approval" not in kept["spec"]
    assert kept["spec"]["image"] == release["spec"]["image"]
    assert kept["metadata"] == {"name": release["metadata"]["name"], "namespace": NAMESPACE}
    assert set(kept_agent) == {"apiVersion", "kind", "metadata", "spec"}
    assert kept_agent["metadata"] == {"name": AGENT, "namespace": NAMESPACE}


def test_a_release_record_is_made_from_its_document():
    _, releases = fixture_documents()

    record = derive.agent_release_record(releases[0], recorded_at=T0, lineage={"tag": "t"})

    assert record.id == f"agent:{AGENT}:{releases[0]['metadata']['name']}"
    assert record.digest == "sha256:" + releases[0]["spec"]["image"].rsplit("sha256:", 1)[1]
    assert record.document == canonical.release_document(releases[0])
    assert record.lineage == {"tag": "t"}
    assert derive.image_digest("registry.example/agents/x:1.0") == ""


def test_requirements_are_the_backends_an_agent_names():
    agent, _ = fixture_documents()

    rows = derive.requirements_of("agent:db-admin:r", agent)

    assert [(r.type, r.target, r.names) for r in rows] == [
        ("model", "llm-main", ["Qwen3.6-35B-A3B-NVFP4"]),
        ("tool", "postgres-admin", ["execute_sql"]),
    ]
    assert rows[1].detail == {"effect": "remote_read", "delegation": "inherit"}


def test_freshness_has_four_answers():
    world = World()
    world.seed()
    seen = world.cluster_shows(1)
    gone = seen.model_copy(update={"present": False})

    assert derive.freshness(None, T0, 180) == "missing"
    assert derive.freshness(seen, T0 + timedelta(seconds=180), 180) == "fresh"
    assert derive.freshness(seen, T0 + timedelta(seconds=181), 180) == "old"
    assert derive.freshness(gone, T0, 180) == "absent"
    assert derive.age_seconds(seen, T0 + timedelta(seconds=7)) == 7
    assert derive.age_seconds(None, T0) is None


def test_what_is_read_from_an_observed_object():
    world = World()
    world.seed()
    world.take()
    seen = world.cluster_shows(2).document

    assert derive.live_generation(seen) == world.store.revision(DEP, 2).generation
    assert derive.live_generation({"metadata": {"annotations": {}}}) is None
    assert (
        derive.live_generation(
            {"metadata": {"annotations": {ANNOTATION_PREFIX + "generation": "x"}}}
        )
        is None
    )
    assert derive.condition(seen, "Ready")["status"] == "True"
    assert derive.condition(seen, "Missing") == {}
    assert derive.pin_for(world.store, CLUSTER, TENANT, AGENT).repository == "X-McKay/zetra-agents"
    assert derive.pin_for(world.store, CLUSTER, TENANT, "unknown") is None
    assert derive.pin_for(world.store, "elsewhere", TENANT, AGENT) is None
