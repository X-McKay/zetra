"""Two requests at once. The other replica is played by a callback that runs once, just before
the transaction of the request under test opens: whatever it writes is committed by the time
the request decides. A request that decided on an earlier plain read would act on a state
that is gone; these tests fail for it."""

from __future__ import annotations

import pytest

from agent_registry import modes
from agent_registry.models import Deployment, Tenant, deployment_id
from agent_registry.rules import approval as approvals
from agent_registry.rules import lifecycle
from tests.test_write_cases import (
    APPROVALS,
    ENABLED,
    PIN,
    RELEASES,
    advance,
    first_release_live,
    last_event,
    one_more_tool,
    preview,
    record,
    refused,
    report,
    types,
)
from tests.world import CLUSTER, DEP, NAMESPACE, TENANT, World, published, registry_owned


def race(world: World, callback) -> None:
    """Run `callback` once, just before the next transaction opens."""
    real = world.store.transaction
    state = {"armed": True}

    def transaction(actor: str):
        if state["armed"]:
            state["armed"] = False
            callback()
        return real(actor)

    world.store.transaction = transaction


def record_directly(world: World, letter: str, change=None) -> int:
    body = published(letter, change)
    with world.store.transaction("other-replica") as tx:
        return lifecycle.record_release(
            tx, DEP, body["agent"], body["agentRelease"], body["lineage"], now=world.now
        )[1]


def approve_directly(world: World, number: int, approval_id: str) -> None:
    seen = preview(world, number)
    with world.store.transaction("other-replica") as tx:
        approvals.approve(
            tx,
            DEP,
            approval_id=approval_id,
            revision=number,
            authority_digest=seen["authorityDigest"],
            base_authority_digest=seen["baseAuthorityDigest"],
            policy_revision_seen=seen["policyRevision"],
            ticket=None,
            approver="other@example.com",
            now=world.now,
            max_age_seconds=world.settings.observation_max_age_seconds,
        )


def approval_body(seen: dict, approval_id: str) -> dict:
    return {
        "approvalId": approval_id,
        "revision": seen["revision"],
        "authorityDigest": seen["authorityDigest"],
        "baseAuthorityDigest": seen["baseAuthorityDigest"],
        "policyRevision": seen["policyRevision"],
    }


def pending(world: World, letter: str = "a") -> tuple[int, dict]:
    number = record(world, letter)
    report(world, number)
    return number, preview(world, number)


def test_approve_races_a_newer_release():
    world = registry_owned()
    number, seen = pending(world)
    generation = {}

    def other():
        record_directly(world, "b")
        generation["after"] = world.store.generation()

    race(world, other)
    answer = world.post(APPROVALS, approval_body(seen, "approval-0001"))

    refused(answer, "stale-approval")
    revision = world.store.revision(DEP, number)
    assert revision.phase == "superseded" and revision.approval_id is None
    assert "approved" not in types(world)
    assert [a for a in world.store.approvals(DEP) if a.origin == "registry"] == []
    assert world.store.generation() == generation["after"]


def test_two_approvals_of_one_revision_with_different_ids():
    world = registry_owned()
    number, seen = pending(world)
    generation = {}

    def other():
        approve_directly(world, number, "approval-0001")
        generation["after"] = world.store.generation()

    race(world, other)
    answer = world.post(APPROVALS, approval_body(seen, "approval-0002"))

    refused(answer, "stale-approval")
    assert types(world).count("approved") == 1
    assert world.store.revision(DEP, number).approval_id == "approval-0001"
    assert world.store.approval("approval-0002") is None
    assert world.store.generation() == generation["after"]


def test_approve_races_a_tenant_policy_change():
    world = registry_owned()
    number, seen = pending(world)
    generation = {}

    def other():
        world.change_policy()
        generation["after"] = world.store.generation()

    race(world, other)
    answer = world.post(APPROVALS, approval_body(seen, "approval-0001"))

    refused(answer, "stale-approval")
    assert "approved" not in types(world)
    assert world.store.revision(DEP, number).approval_id is None
    assert world.store.generation() == generation["after"]


@pytest.mark.parametrize("route", ["releases", "approvals", "enabled", "pin"])
def test_a_write_races_recovery_being_entered(route):
    world = registry_owned()
    number, seen = pending(world)
    generation = {}

    def other():
        modes.enter_recovery(world.store, "a test", actor="other-replica")
        generation["after"] = world.store.generation()

    requests = {
        "releases": lambda: world.post(RELEASES, published("b")),
        "approvals": lambda: world.post(APPROVALS, approval_body(seen, "approval-0001")),
        "enabled": lambda: world.post(ENABLED, {"enabled": False}),
        "pin": lambda: world.post(PIN, {"revision": 1}),
    }
    race(world, other)

    message = refused(requests[route](), "in-recovery")

    assert "a test" in message
    assert world.store.generation() == generation["after"]
    assert world.store.revision(DEP, number).approval_id is None
    assert len(world.store.revisions(DEP)) == 3
    assert world.store.deployment(DEP).enabled is True


def test_record_races_the_same_record():
    world = registry_owned()
    race(world, lambda: record_directly(world, "a"))

    answer = world.post(RELEASES, published("a"))

    assert answer.status_code == 200, answer.text
    assert answer.json()["created"] is False
    assert answer.json()["revision"]["number"] == 3
    assert types(world).count("release-recorded") == 1
    assert len(world.store.revisions(DEP)) == 3


def test_record_races_a_different_release():
    world = registry_owned()
    race(world, lambda: record_directly(world, "a"))

    answer = world.post(RELEASES, published("b"))

    assert answer.status_code == 200, answer.text
    assert answer.json()["created"] is True
    assert answer.json()["revision"]["number"] == 4
    assert [r.phase for r in world.store.revisions(DEP)][2:] == ["superseded", "awaiting-runtime"]
    assert types(world).count("release-recorded") == 2


def test_pin_races_a_disable():
    world = registry_owned()
    race(world, lambda: world.post(ENABLED, {"enabled": False}))

    answer = world.post(PIN, {"revision": 2})

    assert answer.status_code == 200, answer.text
    deployment = world.store.deployment(DEP)
    assert deployment.enabled is False and deployment.pinned_revision == 2
    assert deployment.generation == last_event(world, "pinned").seq


def test_disable_races_a_pin():
    world = registry_owned()
    race(world, lambda: world.post(PIN, {"revision": 2}))

    answer = world.post(ENABLED, {"enabled": False})

    assert answer.status_code == 200, answer.text
    deployment = world.store.deployment(DEP)
    assert deployment.enabled is False and deployment.pinned_revision == 2
    assert deployment.generation == last_event(world, "disabled").seq


def advancing_twice(world: World, event: str) -> None:
    """`advance` decides something would change; before its transaction opens another replica
    runs `advance` and does it. The first call must then write nothing."""
    race(world, lambda: advance(world))
    before = types(world).count(event)
    assert advance(world) is False
    assert types(world).count(event) == before + 1


def test_advance_twice_for_the_first_verdict():
    world = registry_owned()
    number = record(world, "a")
    world.tick()
    world.cluster_shows(number)

    advancing_twice(world, "approval-needed")

    assert world.store.revision(DEP, number).phase == "pending-approval"


def test_advance_twice_for_a_carried_forward_approval():
    world = registry_owned()
    first_release_live(world)
    number = record(world, "b")
    world.tick()
    world.cluster_shows(number)

    advancing_twice(world, "approval-carried-forward")

    assert world.store.revision(DEP, number).approval_id == "approval-0001"


def test_advance_twice_for_an_activation():
    world = registry_owned()
    first_release_live(world)
    number = record(world, "b")
    report(world, number)  # carried forward
    world.tick()
    world.cluster_shows(number)

    advancing_twice(world, "revision-activated")

    assert world.store.deployment(DEP).current_revision == number


def test_the_reason_of_a_waiting_revision_follows_the_tenant_policy():
    world = registry_owned()
    first_release_live(world)
    number = record(world, "b", one_more_tool)
    report(world, number)
    assert "list_tables" in world.store.revision(DEP, number).reason
    needed = types(world).count("approval-needed")
    world.change_policy()
    world.tick()
    world.cluster_shows(number)

    assert advance(world) is True

    revision = world.store.revision(DEP, number)
    assert (revision.phase, revision.reason) == (
        "pending-approval",
        "the tenant policy changed since the last approval",
    )
    assert last_event(world, "approval-needed").detail["reason"] == revision.reason
    assert types(world).count("approval-needed") == needed + 1
    generation = world.store.generation()
    assert advance(world) is False
    assert advance(world) is False
    assert world.store.generation() == generation


def test_a_release_recorded_before_for_another_deployment_of_the_agent():
    world = registry_owned()
    record(world, "a")
    other = deployment_id("agent", CLUSTER, "tenant-other", "db-admin")
    with world.store.transaction("test") as tx:
        tx.put_tenant(
            Tenant(cluster=CLUSTER, name="tenant-other", namespace=NAMESPACE, policy_revision="p")
        )
        tx.put_deployment(
            Deployment(
                id=other,
                kind="agent",
                item="db-admin",
                cluster=CLUSTER,
                tenant="tenant-other",
                namespace=NAMESPACE,
                authority="registry",
                document={"agent": world.store.deployment(DEP).document["agent"]},
            )
        )
        tx.event("test-setup", deployment_id=other)

    answer = world.post(f"/v1/deployments/{other}/releases", published("a"))

    assert answer.status_code == 200, answer.text
    assert answer.json()["created"] is True
    assert answer.json()["revision"]["number"] == 1
    assert answer.json()["revision"]["policyRevision"] == "p"
    assert TENANT not in answer.json()["revision"]["deploymentId"]
