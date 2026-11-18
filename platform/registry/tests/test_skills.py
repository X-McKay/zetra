"""Skills in the catalog and effects in authority (`contract/skills.md` sections 7.4 and 11.2)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from agent_registry import canonical, derive, importer
from agent_registry.rules.authority import STATEMENTS, widens
from tests.world import CLUSTER, DEP, T0, fixture_documents, published, registry_owned

CONTRACT = Path(__file__).parents[1] / "contract"

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
SENTENCE = (
    "effect sandbox_execute: skill scripts run inside the agent's container "
    "and can read the session's identity token"
)


def entry(digest: str = DIGEST_A, **changes) -> dict:
    return {
        "name": "row-count",
        "version": "1.2",
        "digest": digest,
        "description": "Counts rows in a table.",
        "declared": True,
        "scripts": ["scripts/count.py"],
        "interpreters": ["python3"],
    } | changes


def with_skills(body: dict, *skills: dict) -> dict:
    body["agentRelease"]["spec"]["skills"] = list(skills)
    return body


def record(world, body: dict):
    answer = world.post(f"/v1/deployments/{DEP}/releases", body)
    assert answer.status_code == 200, answer.text
    return answer.json()


def skill_releases(world, name: str = "row-count"):
    return [r for r in world.store.releases("skill", name)]


def other_agent_release(skills: list[dict], name: str = "investigator") -> list[dict]:
    """The documents of a fixture agent, its release carrying `skills`."""
    agent, releases = fixture_documents(name)
    release = copy.deepcopy(releases[0])
    release["spec"]["skills"] = skills
    return [agent, release]


def test_skills_become_items_and_releases():
    world = registry_owned()

    record(world, with_skills(published("c"), entry()))

    item = world.store.item("skill", "row-count")
    assert (item.owner, item.description) == (
        world.store.item("agent", "db-admin").owner,
        "Counts rows in a table.",
    )
    (release,) = skill_releases(world)
    assert release.id == f"skill:row-count:sha256-{'a' * 64}"
    assert (release.name, release.digest, release.version, release.origin) == (
        f"sha256-{'a' * 64}",
        DIGEST_A,
        "1.2",
        "built",
    )
    assert release.document == entry()
    assert release.lineage["tag"] == "db-admin/v0.2.12"
    body = world.get("/v1/items/skill/row-count").json()
    assert [r["digest"] for r in body["releases"]] == [DIGEST_A]


def test_a_skill_digest_already_recorded_adds_nothing_and_a_second_digest_adds_a_release():
    world = registry_owned()
    record(world, with_skills(published("c"), entry()))
    before = world.store.release(f"skill:row-count:sha256-{'a' * 64}")

    record(world, with_skills(published("d"), entry(description="New words.")))
    record(world, with_skills(published("e"), entry(DIGEST_B, version="1.3")))

    assert [r.digest for r in skill_releases(world)] == [DIGEST_A, DIGEST_B]
    assert world.store.release(before.id) == before
    # Later releases update the description and nothing else.
    item = world.store.item("skill", "row-count")
    assert item.description == "Counts rows in a table."
    assert item.owner == world.store.item("agent", "db-admin").owner


def test_two_agents_with_one_skill_digest_make_one_release_and_both_are_users():
    world = registry_owned()
    result = importer.import_documents(
        world.store,
        other_agent_release([entry()]) + other_agent_release([entry()], "conformance"),
        cluster=CLUSTER,
        now=T0,
    )

    assert result.problems == []
    assert len(skill_releases(world)) == 1
    body = world.get("/v1/dependants", cluster=CLUSTER, type="skill", name="row-count").json()
    assert (body["target"], body["providedBy"]) == ("row-count", None)
    assert sorted((d["agent"], d["names"]) for d in body["dependants"]) == [
        ("conformance", [DIGEST_A]),
        ("investigator", [DIGEST_A]),
    ]
    nobody = world.get("/v1/dependants", cluster=CLUSTER, type="skill", name="other").json()
    assert nobody["dependants"] == []


def test_the_cluster_import_records_skills_once():
    world = registry_owned()
    documents = other_agent_release([entry()])

    first = importer.import_documents(world.store, documents, cluster=CLUSTER, now=T0)
    second = importer.import_documents(world.store, documents, cluster=CLUSTER, now=T0)

    assert f"skill:row-count:sha256-{'a' * 64}" in first.created
    assert not second.changed
    assert [r.digest for r in skill_releases(world)] == [DIGEST_A]


def test_requirements_name_skills_and_effects():
    agent, _ = fixture_documents()
    agent = copy.deepcopy(agent)
    agent["spec"]["capabilities"]["effects"] = ["sandbox_execute"]

    rows = derive.requirements_of("agent:db-admin:r", agent, [entry()])

    assert [(r.type, r.target, r.names) for r in rows] == [
        ("effect", "sandbox_execute", []),
        ("model", "llm-main", ["Qwen3.6-35B-A3B-NVFP4"]),
        ("skill", "row-count", [DIGEST_A]),
        ("tool", "postgres-admin", ["execute_sql"]),
    ]


def test_a_release_without_skills_or_effects_records_nothing_new():
    world = registry_owned()
    items, releases = world.store.items(), world.store.releases()
    agent, _ = fixture_documents()

    record(world, published("c"))

    assert [i.kind for i in world.store.items()] == [i.kind for i in items]
    assert len(world.store.releases()) == len(releases) + 1
    assert world.store.items("skill") == []
    types = {
        r.type for r in world.store.requirements(f"agent:db-admin:db-admin-2026-11-18-{'c' * 8}")
    }
    assert types == {"model", "tool"}
    assert "effects" not in canonical.authority_of(agent)


def test_requesting_the_effect_changes_authority_and_widens_it():
    agent, _ = fixture_documents()
    base = canonical.authority_of(agent)
    asking = copy.deepcopy(agent)
    asking["spec"]["capabilities"]["effects"] = ["sandbox_execute"]

    authority = canonical.authority_of(asking)

    assert authority == base | {"effects": ["sandbox_execute"]}
    assert canonical.authority_digest(authority) != canonical.authority_digest(base)
    assert widens(base, authority) == [SENTENCE]
    assert widens(authority, authority) == []
    assert widens(authority, base) == []


def test_the_approval_of_a_release_that_asks_for_the_effect_shows_the_sentence():
    world = registry_owned()
    body = published("c", lambda spec: spec["capabilities"].update(effects=["sandbox_execute"]))
    number = record(world, body)["revision"]["number"]

    shown = world.get(f"/v1/deployments/{DEP}/revisions/{number}/approval")

    assert shown.status_code == 200, shown.text
    assert SENTENCE in json.dumps(shown.json())


def test_the_registrys_statement_is_the_one_in_effects_json():
    effects = json.loads((CONTRACT / "effects.json").read_text())["effects"]

    assert STATEMENTS == {name: found["statement"] for name, found in effects.items()}


def test_a_release_with_a_malformed_skill_list_is_refused():
    world = registry_owned()
    twice = [entry(), entry(DIGEST_B)]
    for skills in ([], "row-count", [entry(digest="sha256:abc")], [{"digest": DIGEST_A}], twice):
        body = published("c")
        body["agentRelease"]["spec"]["skills"] = skills
        answer = world.post(f"/v1/deployments/{DEP}/releases", body)
        assert (answer.status_code, answer.json()["error"]["code"]) == (400, "invalid-arguments")
    assert world.store.item("skill", "row-count") is None
