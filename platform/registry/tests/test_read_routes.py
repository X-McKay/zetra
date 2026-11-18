"""The read routes other than status: records, dependants, events, and the observed state of
tool servers and models."""

from __future__ import annotations

import json
from agent_registry import importer
from agent_registry.models import Observation
from tests.world import CLUSTER, DEP, FIXTURES, T0, World, registry_owned

MANIFESTS = FIXTURES / "manifests"
TOOL_SERVER = f"tool-server:{CLUSTER}:-:postgres-admin-mcp"
MODEL = f"model:{CLUSTER}:-:main"


def fed() -> World:
    """The catalog feed (tool servers, models, aliases) and every fixture agent."""
    world = World()
    importer.apply_feed(world.store, json.loads((FIXTURES / "feed.json").read_text()), now=T0)
    importer.import_documents(
        world.store, importer.load_directory(MANIFESTS), cluster=CLUSTER, now=T0
    )
    return world


def error_code(answer, status: int) -> str:
    assert answer.status_code == status, answer.text
    return answer.json()["error"]["code"]


# -- dependants ----------------------------------------------------------------------------


def test_the_dependants_of_a_tool_backend():
    answer = fed().get("/v1/dependants", cluster=CLUSTER, type="tool", name="postgres-admin")

    body = answer.json()
    assert (body["target"], body["providedBy"]) == ("postgres-admin", TOOL_SERVER)
    assert [(d["agent"], d["names"]) for d in body["dependants"]] == [("db-admin", ["execute_sql"])]
    first = body["dependants"][0]
    assert first["deployment"] == DEP
    assert (first["revision"], first["release"]) == (2, "db-admin-2026-11-13-33c8acb2")


def test_the_dependants_of_a_model_alias():
    body = fed().get("/v1/dependants", cluster=CLUSTER, type="model", name="main").json()

    assert (body["target"], body["providedBy"]) == ("llm-main", MODEL)
    assert "db-admin" in [d["agent"] for d in body["dependants"]]
    db_admin = next(d for d in body["dependants"] if d["agent"] == "db-admin")
    assert db_admin["names"] == ["Qwen3.6-35B-A3B-NVFP4"]


def test_an_unknown_backend_and_an_unknown_alias_are_empty_answers():
    world = fed()

    tool = world.get("/v1/dependants", cluster=CLUSTER, type="tool", name="nothing").json()
    model = world.get("/v1/dependants", cluster=CLUSTER, type="model", name="nothing").json()

    assert (tool["target"], tool["providedBy"], tool["dependants"]) == ("nothing", None, [])
    assert (model["target"], model["providedBy"], model["dependants"]) == (None, None, [])


# -- tool servers and models in status -----------------------------------------------------


def report(world: World, kind: str, namespace: str, name: str, document: dict, **more) -> None:
    world.store.put_observations(
        CLUSTER,
        [
            Observation(
                cluster=CLUSTER,
                subject_kind=kind,
                namespace=namespace,
                name=name,
                observed_at=world.now,
                document=document,
                **more,
            )
        ],
    )


def tool_server(world: World) -> dict:
    body = world.get("/v1/status", cluster=CLUSTER).json()
    return next(t for t in body["toolServers"] if t["name"] == "postgres-admin-mcp")


def model(world: World) -> dict:
    body = world.get("/v1/status", cluster=CLUSTER).json()
    return next(m for m in body["models"] if m["name"] == "main")


TOOL_REPORT = {"images": ["docker.io/crystaldba/postgres-mcp@sha256:abc"], "readyReplicas": 1}
MODEL_REPORT = {
    "images": ["vllm/vllm-openai:v0.30.0"],
    "readyReplicas": 1,
    "servedName": "Qwen3.6-35B-A3B-NVFP4",
}


def test_a_tool_server_nothing_was_reported_about_is_missing():
    found = tool_server(fed())

    assert found["observation"] == {"state": "missing", "observedAt": None, "ageSeconds": None}
    assert (found["observedImages"], found["readyReplicas"]) == ([], None)
    assert (found["registered"], found["backend"], found["pinnedByDigest"]) == (
        True,
        "postgres-admin",
        True,
    )
    assert found["declaredImage"].startswith("docker.io/crystaldba/postgres-mcp@sha256:")
    assert found["dependants"] == ["db-admin"]


def test_a_tool_server_report_older_than_the_limit_shows_nothing_observed():
    world = fed()
    report(world, "tool-server", "mcp", "postgres-admin-mcp", TOOL_REPORT)
    world.tick(world.settings.observation_max_age_seconds + 20)

    found = tool_server(world)

    assert found["observation"]["state"] == "old"
    assert found["observation"]["ageSeconds"] == world.settings.observation_max_age_seconds + 20
    assert (found["observedImages"], found["readyReplicas"]) == ([], None)


def test_a_fresh_tool_server_report_shows_images_and_ready_replicas():
    world = fed()
    report(world, "tool-server", "mcp", "postgres-admin-mcp", TOOL_REPORT)
    world.tick(5)

    found = tool_server(world)

    assert found["observation"]["state"] == "fresh"
    assert found["observation"]["ageSeconds"] == 5
    assert found["observedImages"] == TOOL_REPORT["images"]
    assert found["readyReplicas"] == 1


def test_a_model_is_shown_the_same_way():
    world = fed()

    missing = model(world)
    assert missing["observation"]["state"] == "missing"
    assert (missing["observedServedName"], missing["observedImage"]) == (None, None)
    assert missing["declaredServedName"] == "Qwen3.6-35B-A3B-NVFP4"
    assert (missing["backend"], missing["aliases"]) == ("llm-main", ["main"])
    assert "db-admin" in missing["dependants"]

    report(world, "model-engine", "vllm", "main", MODEL_REPORT)
    world.tick(world.settings.observation_max_age_seconds + 1)
    old = model(world)
    assert old["observation"]["state"] == "old"
    assert (old["observedServedName"], old["observedImage"]) == (None, None)

    report(world, "model-engine", "vllm", "main", MODEL_REPORT)
    fresh = model(world)
    assert fresh["observation"]["state"] == "fresh"
    assert fresh["observedServedName"] == "Qwen3.6-35B-A3B-NVFP4"
    assert fresh["observedImage"] == "vllm/vllm-openai:v0.30.0"


def test_a_workload_the_provisioner_did_not_find_is_absent():
    world = fed()
    report(world, "tool-server", "mcp", "postgres-admin-mcp", {}, present=False)

    found = tool_server(world)

    assert found["observation"]["state"] == "absent"
    assert (found["observedImages"], found["readyReplicas"]) == ([], None)


def test_status_of_a_cluster_that_does_not_exist_is_not_found():
    assert error_code(World().get("/v1/status", cluster="nowhere"), 404) == "not-found"


# -- records -------------------------------------------------------------------------------


def test_an_item_with_its_releases_and_deployments():
    world = registry_owned()

    body = world.get("/v1/items/agent/db-admin").json()

    assert body["item"]["name"] == "db-admin"
    assert len(body["releases"]) == 2
    assert [d["id"] for d in body["deployments"]] == [DEP]
    assert error_code(world.get("/v1/items/agent/nothing"), 404) == "not-found"
    assert [i["name"] for i in world.get("/v1/items", kind="agent").json()["items"]] == ["db-admin"]
    assert world.get("/v1/items", kind="model").json() == {"items": []}


def test_a_release_with_its_requirements():
    world = registry_owned()
    release = world.store.revision(DEP, 2).release_id

    body = world.get(f"/v1/releases/{release}").json()

    assert body["release"]["id"] == release
    assert [(r["type"], r["target"]) for r in body["requirements"]] == [
        ("model", "llm-main"),
        ("tool", "postgres-admin"),
    ]
    assert error_code(world.get("/v1/releases/agent:db-admin:nothing"), 404) == "not-found"


def test_a_deployment_with_its_revisions_in_order_and_its_approvals():
    world = registry_owned()

    body = world.get(f"/v1/deployments/{DEP}").json()

    assert body["deployment"]["id"] == DEP
    assert [r["number"] for r in body["revisions"]] == [1, 2]
    assert len(body["approvals"]) == 2
    assert error_code(world.get("/v1/deployments/agent:kubani:-:nothing"), 404) == "not-found"


def test_deployments_can_be_filtered_by_authority():
    world = World()
    world.seed()

    def ids(**params) -> list[str]:
        return [d["id"] for d in world.get("/v1/deployments", **params).json()["deployments"]]

    assert ids(authority="pull-request") == [DEP]
    assert ids(authority="registry") == []
    world.take()
    assert ids(authority="registry") == [DEP]
    assert ids(authority="pull-request") == []
    assert ids(kind="agent", cluster=CLUSTER) == [DEP]
    assert ids(kind="model") == []


def test_a_revision_with_its_release_resolutions_and_approval():
    world = registry_owned()

    body = world.get(f"/v1/deployments/{DEP}/revisions/2").json()

    assert body["revision"]["number"] == 2
    assert body["release"]["id"] == body["revision"]["releaseId"]
    assert body["approval"]["id"] == body["revision"]["approvalId"]
    assert {r["type"] for r in body["resolutions"]} == {"model", "tool"}
    assert error_code(world.get(f"/v1/deployments/{DEP}/revisions/9"), 404) == "not-found"
    unknown = world.get("/v1/deployments/agent:kubani:-:nothing/revisions/1")
    assert error_code(unknown, 404) == "not-found"


def test_events_after_a_sequence_number_with_a_limit():
    world = registry_owned()

    everything = world.get("/v1/events").json()
    assert [e["seq"] for e in everything["events"]] == [1, 2]
    assert everything["generation"] == 2

    later = world.get("/v1/events", after=1).json()
    assert [e["type"] for e in later["events"]] == ["authority-taken"]

    one = world.get("/v1/events", limit=1).json()
    assert [e["seq"] for e in one["events"]] == [1]
    assert one["generation"] == 2

    assert world.get("/v1/events", deployment="agent:kubani:-:other").json()["events"] == []
    assert world.get("/v1/meta").json()["generation"] == 2
