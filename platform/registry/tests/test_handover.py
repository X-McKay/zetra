"""Taking authority and giving it back: `contract/projection.md` section 7."""

from __future__ import annotations

import copy

import pytest

from agent_registry import canonical, modes
from agent_registry.config import ANNOTATION_PREFIX
from agent_registry.models import Deployment, Item
from agent_registry.rules import handover
from tests.test_write_cases import record, refused
from tests.test_write_races import race
from tests.world import CLUSTER, DEP, NAMESPACE, World, fixture_documents, registry_owned

TAKE = f"/v1/deployments/{DEP}/authority/take"
RETURN = f"/v1/deployments/{DEP}/authority/return"


def observed_pull_request_world() -> World:
    world = World()
    world.seed()
    world.cluster_shows_everything()
    return world


def snapshot(world: World) -> tuple:
    return (
        world.store.generation(),
        len(world.store.events(limit=10000)),
        world.store.deployment(DEP),
        world.store.revisions(DEP),
        world.store.approvals(DEP),
    )


def edit(world: World, kind: str, name: str, change) -> None:
    found = world.store.observation(CLUSTER, kind, NAMESPACE, name)
    document = copy.deepcopy(found.document)
    change(document)
    world.store.put_observations(CLUSTER, [found.model_copy(update={"document": document})])


def release_names(world: World) -> list[str]:
    return [world.store.release(r.release_id).name for r in world.store.revisions(DEP)]


def test_take_with_fresh_equal_observations():
    world = observed_pull_request_world()

    answer = world.post(TAKE)

    assert answer.status_code == 200, answer.text
    assert answer.json()["changed"] is True
    seq = [e for e in world.store.events(deployment_id=DEP) if e.type == "authority-taken"][-1].seq
    deployment = world.store.deployment(DEP)
    assert deployment.authority == "registry" and deployment.generation == seq
    assert answer.json()["deployment"]["authority"] == "registry"
    assert [r.generation for r in world.store.revisions(DEP)] == [seq, seq]
    assert world.store.generation() == seq


def test_a_flux_inventory_is_not_a_reason_to_refuse():
    world = observed_pull_request_world()
    for found in world.store.observations(CLUSTER):
        world.store.put_observations(
            CLUSTER, [found.model_copy(update={"in_flux_inventory": True})]
        )

    assert world.post(TAKE).status_code == 200


def test_take_twice_writes_nothing_the_second_time():
    world = observed_pull_request_world()
    assert world.post(TAKE).json()["changed"] is True
    before = snapshot(world)

    again = world.post(TAKE)

    assert again.status_code == 200 and again.json()["changed"] is False
    assert snapshot(world) == before


def release_name(world: World, number: int) -> str:
    return world.store.release(world.store.revision(DEP, number).release_id).name


def stale(world: World) -> None:
    world.tick(world.settings.observation_max_age_seconds + 1)


def differing_spec(world: World) -> None:
    edit(world, "agent", "db-admin", lambda d: d["spec"]["runtime"].update(cpu="4"))


def differing_release_spec(world: World) -> None:
    edit(
        world,
        "agentrelease",
        release_name(world, 1),
        lambda d: d["spec"].update(image="registry.almckay.io/agents/db-admin@sha256:" + "0" * 64),
    )


def differing_block(world: World) -> None:
    edit(
        world,
        "agentrelease",
        release_name(world, 2),
        lambda d: d["spec"]["approval"].update(approvedBy="someone@example.com"),
    )


def unapproved_on_the_cluster(world: World) -> None:
    edit(
        world,
        "agentrelease",
        release_name(world, 2),
        lambda d: d["spec"].update(approval={"approved": False}),
    )


def unknown_release(world: World) -> None:
    found = world.store.observation(CLUSTER, "agentrelease", NAMESPACE, release_name(world, 2))
    other = found.model_copy(update={"name": "db-admin-2026-11-18-ffffffff"})
    world.store.put_observations(CLUSTER, [other])


def agent_gone(world: World) -> None:
    world.store.put_observations(
        CLUSTER,
        [world.store.observation(CLUSTER, "agent", NAMESPACE, "db-admin")],
        complete_for=[("agent", NAMESPACE)],
    )
    found = world.store.observation(CLUSTER, "agent", NAMESPACE, "db-admin")
    world.store.put_observations(CLUSTER, [found.model_copy(update={"present": False})])


@pytest.mark.parametrize(
    "spoil",
    [
        stale,
        differing_spec,
        differing_release_spec,
        differing_block,
        unapproved_on_the_cluster,
        unknown_release,
        agent_gone,
    ],
)
def test_take_is_refused_when_the_registry_and_the_cluster_differ(spoil):
    world = observed_pull_request_world()
    spoil(world)
    before = snapshot(world)

    message = refused(world.post(TAKE), "conflict")

    assert "db-admin" in message
    assert snapshot(world) == before


def test_take_is_refused_for_an_agent_never_observed_and_names_every_reason():
    world = World()
    world.seed()

    message = refused(world.post(TAKE), "conflict")

    assert "agent db-admin" in message
    for name in release_names(world):
        assert name in message


def test_take_names_every_reason_not_only_the_first():
    world = observed_pull_request_world()
    differing_spec(world)
    differing_block(world)
    unknown_release(world)

    message = refused(world.post(TAKE), "conflict")

    assert "spec of agent db-admin" in message
    assert f"approval of agentrelease {release_name(world, 2)}" in message
    assert "db-admin-2026-11-18-ffffffff" in message


def test_a_revision_without_an_approval_must_not_be_approved_on_the_cluster():
    world = registry_owned()
    number = record(world, "a")
    deployment = world.store.deployment(DEP)
    world.tick()

    def reasons() -> list[str]:
        return handover.differences(
            world.store,
            deployment,
            now=world.now,
            max_age=world.settings.observation_max_age_seconds,
        )

    world.cluster_shows(number)
    assert reasons() == []
    world.cluster_shows(number, ready=False)
    edit(
        world,
        "agentrelease",
        release_name(world, number),
        lambda d: d["spec"].update(approval={"approved": True, "approvedBy": "x"}),
    )
    assert [r for r in reasons() if "approval" in r] == [
        f"the approval of agentrelease {release_name(world, number)} on the cluster differs"
    ]
    edit(
        world,
        "agentrelease",
        release_name(world, number),
        lambda d: d["spec"].pop("approval"),
    )
    assert reasons() == []


def test_take_for_an_unknown_deployment_or_one_that_is_not_an_agent():
    world = observed_pull_request_world()
    with world.store.transaction("test") as tx:
        tx.put_item(Item(kind="tool-server", name="github-mcp"))
        tx.put_deployment(
            Deployment(
                id="tool-server:kubani:-:github-mcp",
                kind="tool-server",
                item="github-mcp",
                cluster=CLUSTER,
                namespace="mcp",
            )
        )
        tx.event("test-setup")

    for missing in ("agent:kubani:tenant-test:nope", "tool-server:kubani:-:github-mcp"):
        refused(world.post(f"/v1/deployments/{missing}/authority/take"), "not-found", 404)
        refused(world.post(f"/v1/deployments/{missing}/authority/return"), "not-found", 404)


def test_take_races_recovery_being_entered():
    world = observed_pull_request_world()
    generation = {}

    def other():
        modes.enter_recovery(world.store, "a test", actor="other-replica")
        generation["after"] = world.store.generation()

    race(world, other)
    before = world.store.deployment(DEP)

    message = refused(world.post(TAKE), "in-recovery")

    assert "a test" in message
    assert world.store.generation() == generation["after"]
    assert world.store.deployment(DEP) == before
    assert "authority-taken" not in [e.type for e in world.store.events(limit=10000)]


def test_return_after_take_gives_the_manifests_back():
    world = observed_pull_request_world()
    assert world.post(TAKE).status_code == 200
    manifest_agent, manifest_releases = fixture_documents()

    answer = world.post(RETURN)

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["changed"] is True and body["deployment"]["authority"] == "pull-request"
    assert body["agent"] == canonical.agent_document(manifest_agent)
    assert body["agentReleases"] == sorted(
        manifest_releases, key=lambda r: r["spec"]["approval"]["approvedAt"]
    )
    for document in [body["agent"], *body["agentReleases"]]:
        assert "annotations" not in document["metadata"]
    deployment = world.store.deployment(DEP)
    assert deployment.authority == "pull-request"
    assert [r.number for r in world.store.revisions(DEP)] == [1, 2]
    assert deployment.current_revision == 2
    assert not any(
        key.startswith(ANNOTATION_PREFIX) for key in deployment.document["agent"]["metadata"]
    )


def test_return_twice_writes_nothing_the_second_time():
    world = registry_owned()
    assert world.post(RETURN).json()["changed"] is True
    before = snapshot(world)

    again = world.post(RETURN)

    assert again.status_code == 200 and again.json()["changed"] is False
    assert again.json()["agentReleases"]
    assert snapshot(world) == before


def test_return_for_an_agent_still_on_the_pull_request_path_changes_nothing():
    world = observed_pull_request_world()
    before = snapshot(world)

    answer = world.post(RETURN)

    assert answer.status_code == 200 and answer.json()["changed"] is False
    assert snapshot(world) == before


def test_return_races_recovery_being_entered():
    world = registry_owned()
    race(world, lambda: modes.enter_recovery(world.store, "a test", actor="other-replica"))

    refused(world.post(RETURN), "in-recovery")

    assert world.store.deployment(DEP).authority == "registry"


def test_an_agent_taken_while_disabled_can_be_enabled_by_the_registry():
    world = World()
    world.seed()
    deployment = world.store.deployment(DEP)
    agent = copy.deepcopy(deployment.document["agent"])
    agent["spec"]["enabled"] = False
    with world.store.transaction("test") as tx:  # as the importer records a disabled manifest
        tx.put_deployment(
            deployment.model_copy(update={"enabled": False, "document": {"agent": agent}})
        )
        tx.event("test-setup")
    world.cluster_shows_everything()
    enabled = f"/v1/deployments/{DEP}/enabled"

    assert world.post(TAKE).status_code == 200
    taken = world.store.deployment(DEP)
    assert taken.enabled is False and taken.document["agent"]["spec"]["enabled"] is True
    projected = world.get(f"/v1/clusters/{CLUSTER}/desired", token="provisioner").json()
    assert projected["deployments"][0]["objects"][0]["document"]["spec"]["enabled"] is False

    assert world.post(enabled, {"enabled": True}).status_code == 200
    projected = world.get(f"/v1/clusters/{CLUSTER}/desired", token="provisioner").json()
    assert projected["deployments"][0]["objects"][0]["document"]["spec"]["enabled"] is True

    assert world.post(enabled, {"enabled": False, "reason": "back as found"}).status_code == 200
    assert world.post(RETURN).json()["agent"]["spec"]["enabled"] is False
