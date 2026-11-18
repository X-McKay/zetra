"""What a cluster is given: `contract/projection.md` sections 1 to 4, through the HTTP
application."""

from __future__ import annotations

import json
import re

from agent_registry import canonical, modes
from agent_registry.api import schemas as s
from agent_registry.config import ANNOTATION_PREFIX, FIELD_MANAGER
from agent_registry.models import Deployment, Item, deployment_id
from agent_registry.rules import projection
from tests.test_write_cases import approve, first_release_live, record, report
from tests.world import (
    CLUSTER,
    DEP,
    NAMESPACE,
    World,
    fixture_documents,
    registry_owned,
)

DESIRED = f"/v1/clusters/{CLUSTER}/desired"
PREFIX = ANNOTATION_PREFIX


def desired(world: World, **params) -> dict:
    answer = world.get(DESIRED, token="provisioner", **params)
    assert answer.status_code == 200, answer.text
    return answer.json()


def objects(world: World) -> list[dict]:
    (found,) = desired(world)["deployments"]
    return found["objects"]


def spec_approval(entry: dict) -> dict:
    return entry["document"]["spec"]["approval"]


def test_the_projection_of_a_registry_owned_agent():
    world = registry_owned()

    answer = desired(world)

    parsed = s.DesiredOut.model_validate(answer)
    assert (parsed.cluster, parsed.mode, parsed.field_manager) == (CLUSTER, "normal", FIELD_MANAGER)
    assert parsed.generation == world.store.generation()
    assert parsed.namespaces == [NAMESPACE]
    assert parsed.flux_inventories == world.store.cluster(CLUSTER).flux_inventories
    (found,) = answer["deployments"]
    assert (found["deployment"], found["agent"], found["tenant"]) == (
        DEP,
        "db-admin",
        "tenant-test",
    )
    assert [(o["kind"], o["name"]) for o in found["objects"]] == [
        ("Agent", "db-admin"),
        *[("AgentRelease", r.name) for r in map(world.store.release, release_ids(world))],
    ]
    deployment = world.store.deployment(DEP)
    agent, *releases = found["objects"]
    assert agent["generation"] == deployment.generation
    assert agent["document"]["metadata"]["annotations"] == {
        PREFIX + "deployment": DEP,
        PREFIX + "cluster": CLUSTER,
        PREFIX + "generation": str(deployment.generation),
        PREFIX + "current-release": releases[-1]["name"],
    }
    for number, entry in enumerate(releases, start=1):
        revision = world.store.revision(DEP, number)
        assert entry["generation"] == revision.generation
        assert entry["document"]["metadata"]["annotations"] == {
            PREFIX + "deployment": DEP,
            PREFIX + "cluster": CLUSTER,
            PREFIX + "generation": str(revision.generation),
            PREFIX + "revision": str(number),
        }
    assert [o.document for o in projection.objects_of(world.store, deployment)] == [
        o["document"] for o in found["objects"]
    ]


def release_ids(world: World) -> list[str]:
    return [r.release_id for r in world.store.revisions(DEP)]


def test_an_agent_with_no_current_release_has_no_such_annotation():
    world = World()
    world.seed()
    with world.store.transaction("test") as tx:
        tx.put_deployment(
            tx.deployment(DEP).model_copy(
                update={"authority": "registry", "current_revision": None}
            )
        )
        tx.event("test-setup")

    (agent, *_) = objects(world)

    assert PREFIX + "current-release" not in agent["document"]["metadata"]["annotations"]


def test_an_imported_agent_after_take_differs_from_its_manifests_by_annotations_only():
    world = registry_owned()
    manifest_agent, manifest_releases = fixture_documents()
    expected = {r["metadata"]["name"]: r for r in manifest_releases}

    agent, *releases = objects(world)

    document = agent["document"]
    assert document["metadata"]["annotations"]
    assert projection.without_annotations(document) == canonical.agent_document(manifest_agent)
    assert len(releases) == len(expected) == 2
    for entry in releases:
        manifest = expected[entry["name"]]
        assert json.dumps(spec_approval(entry)) == json.dumps(manifest["spec"]["approval"])
        assert projection.without_annotations(entry["document"]) == manifest
        assert set(entry["document"]["metadata"]) == {"name", "namespace", "annotations"}
        assert set(entry["document"]["metadata"]["annotations"]) == {
            PREFIX + key for key in ("deployment", "cluster", "generation", "revision")
        }


def test_an_unapproved_revision_projects_an_unapproved_block():
    world = registry_owned()
    record(world, "a")

    entry = objects(world)[-1]

    assert spec_approval(entry) == {"approved": False}


def test_a_registry_approval_is_the_four_key_block():
    world = registry_owned()
    number = record(world, "a")
    assert report(world, number) is True
    world.tick(0.5)

    assert approve(world, number, ticket="CHG-1").status_code == 200

    block = spec_approval(objects(world)[-1])
    assert list(block) == ["approved", "approvedBy", "approvedAt", "ticket"]
    assert block["approved"] is True and block["ticket"] == "CHG-1"
    assert block["approvedBy"] == "al@example.com"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", block["approvedAt"])


def test_a_registry_approval_without_a_ticket_has_no_ticket_key():
    world = registry_owned()
    first_release_live(world)

    block = spec_approval(objects(world)[-1])

    assert set(block) == {"approved", "approvedBy", "approvedAt"}


def test_a_superseded_approval_still_projects_on_the_releases_it_approved():
    world = registry_owned()
    number = first_release_live(world)
    approval_id = world.store.revision(DEP, number).approval_id
    with world.store.transaction("test") as tx:
        tx.set_approval_state(approval_id, "superseded")
        tx.event("test-setup")

    assert spec_approval(objects(world)[-1])["approved"] is True


def test_a_disabled_deployment_projects_a_disabled_agent():
    world = registry_owned()

    assert world.post(f"/v1/deployments/{DEP}/enabled", {"enabled": False}).status_code == 200

    agent = objects(world)[0]
    assert agent["document"]["spec"]["enabled"] is False
    deployment = world.store.deployment(DEP)
    assert agent["generation"] == deployment.generation


def test_a_document_that_is_disabled_itself_stays_disabled_when_the_deployment_is_enabled():
    world = registry_owned()
    deployment = world.store.deployment(DEP)
    agent = {**deployment.document["agent"], "spec": {**deployment.document["agent"]["spec"]}}
    agent["spec"]["enabled"] = False
    with world.store.transaction("test") as tx:
        tx.put_deployment(deployment.model_copy(update={"document": {"agent": agent}}))
        tx.event("test-setup")

    assert objects(world)[0]["document"]["spec"]["enabled"] is False
    assert world.store.deployment(DEP).document["agent"]["spec"]["enabled"] is False


def test_a_provisioner_of_another_cluster_is_refused():
    world = registry_owned()

    answer = world.get("/v1/clusters/elsewhere/desired", token="provisioner")

    assert answer.status_code == 403
    assert answer.json()["error"]["code"] == "refused"


def test_a_last_generation_above_the_registrys_enters_recovery():
    world = registry_owned()
    generation = world.store.generation()

    answer = desired(world, lastGeneration=generation + 1)

    assert answer["mode"] == "recovery"
    assert answer["deployments"] == []
    assert answer["namespaces"] == [NAMESPACE]
    meta = world.store.meta()
    assert meta.mode == "recovery"
    assert meta.mode_reason == (
        f"cluster {CLUSTER} has seen generation {generation + 1}; the registry is at {generation}"
    )
    again = desired(world, lastGeneration=generation + 1)
    assert again["mode"] == "recovery" and again["deployments"] == []


def test_a_last_generation_equal_to_the_registrys_is_normal():
    world = registry_owned()

    answer = desired(world, lastGeneration=world.store.generation())

    assert answer["mode"] == "normal" and len(answer["deployments"]) == 1


def test_in_recovery_namespaces_and_observe_are_still_given():
    world = registry_owned()
    add_workloads(world)
    modes.enter_recovery(world.store, "a test", actor="test")

    answer = desired(world)

    assert answer["mode"] == "recovery" and answer["deployments"] == []
    assert answer["namespaces"] == [NAMESPACE]
    assert answer["fluxInventories"]
    assert len(answer["observe"]) == 2


def add_workloads(world: World) -> None:
    with world.store.transaction("test") as tx:
        for kind, name, document in (
            (
                "tool-server",
                "github-mcp",
                {
                    "namespace": "mcp",
                    "workload": {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "name": "github-mcp",
                    },
                    "image": "ghcr.io/github/github-mcp-server:v2.0.2",
                },
            ),
            (
                "model",
                "main",
                {
                    "namespace": "vllm",
                    "workload": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "vllm"},
                    "servedName": "x",
                    "servedNameFrom": {"kind": "ConfigMap", "name": "model-config", "key": "K"},
                },
            ),
        ):
            tx.put_item(Item(kind=kind, name=name))
            tx.put_deployment(
                Deployment(
                    id=deployment_id(kind, CLUSTER, None, name),
                    kind=kind,
                    item=name,
                    cluster=CLUSTER,
                    namespace=document["namespace"],
                    document=document,
                )
            )
        tx.event("test-setup")


def test_observe_lists_tool_servers_and_model_engines_with_their_workloads():
    world = registry_owned()
    add_workloads(world)

    answer = desired(world)

    assert answer["observe"] == [
        {
            "subjectKind": "tool-server",
            "name": "github-mcp",
            "namespace": "mcp",
            "workload": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "namespace": "mcp",
                "name": "github-mcp",
            },
            "servedNameFrom": None,
        },
        {
            "subjectKind": "model-engine",
            "name": "main",
            "namespace": "vllm",
            "workload": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "namespace": "vllm",
                "name": "vllm",
            },
            "servedNameFrom": {"kind": "ConfigMap", "name": "model-config", "key": "K"},
        },
    ]


def test_only_registry_owned_agents_are_given():
    world = World()
    world.seed()

    answer = desired(world)

    assert answer["deployments"] == [] and answer["mode"] == "normal"
