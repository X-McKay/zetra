"""The cases of `contract/lifecycle.md` section 10, through the HTTP application.

The cluster's side is played by `tests/world.py`. The observation intake route belongs to
another package, so these tests store an observation and call `lifecycle.advance` themselves,
which is all the intake does with the lifecycle.

Skipped until `agent_registry/rules/lifecycle.py` exists. Tests may be added to this file;
the ones here are the contract and are not changed to make them pass.
"""

from __future__ import annotations

import copy

import pytest

from agent_registry import canonical, derive, modes
from agent_registry.rules import gates
from tests.world import (
    AGENT,
    CLUSTER,
    DEP,
    NAMESPACE,
    PERSON,
    TENANT,
    World,
    published,
    registry_owned,
    verdict,
)

lifecycle = pytest.importorskip("agent_registry.rules.lifecycle")

RELEASES = f"/v1/deployments/{DEP}/releases"
APPROVALS = f"/v1/deployments/{DEP}/approvals"
ENABLED = f"/v1/deployments/{DEP}/enabled"
PIN = f"/v1/deployments/{DEP}/pin"
WAITING = "waiting for the controller's verdict"
APPROVED_WAITING = "approved; waiting for Ready"


# -- helpers -------------------------------------------------------------------------------


def advance(world: World) -> bool:
    return lifecycle.advance(
        world.store,
        DEP,
        now=world.now,
        max_age_seconds=world.settings.observation_max_age_seconds,
    )


def record(world: World, letter: str, change=None) -> int:
    """Record a release; returns its revision number."""
    answer = world.post(RELEASES, published(letter, change))
    assert answer.status_code == 200, answer.text
    assert answer.json()["created"] is True
    return answer.json()["revision"]["number"]


def report(world: World, number: int, **how) -> bool:
    """The cluster reports a revision's AgentRelease a second later, and the registry reacts."""
    world.tick()
    world.cluster_shows(number, **how)
    return advance(world)


def preview(world: World, number: int) -> dict:
    answer = world.get(f"/v1/deployments/{DEP}/revisions/{number}/approval")
    assert answer.status_code == 200, answer.text
    return answer.json()


def approve(world: World, number: int, approval_id: str = "approval-0001", **overrides):
    """Approve as the client does: read the preview and send its four fields back."""
    seen = preview(world, number)
    body = {
        "approvalId": approval_id,
        "revision": seen["revision"],
        "authorityDigest": seen["authorityDigest"],
        "baseAuthorityDigest": seen["baseAuthorityDigest"],
        "policyRevision": seen["policyRevision"],
    }
    return world.post(APPROVALS, body | overrides)


def first_release_live(world: World) -> int:
    """Release `a` recorded, approved by a person and activated. Returns its revision (3)."""
    number = record(world, "a")
    assert report(world, number) is True
    assert approve(world, number).status_code == 200
    assert report(world, number) is True
    assert world.store.revision(DEP, number).phase == "activated"
    return number


def one_more_tool(spec: dict) -> None:
    spec["capabilities"]["mcp"][0]["tools"].append("list_tables")


def lower_budget(spec: dict) -> None:
    spec["capabilities"]["models"][0]["budget"]["tokensPerDay"] = 500000


def snapshot(world: World) -> tuple:
    """Everything a refused write must leave as it was."""
    return (
        world.store.generation(),
        len(world.store.events(limit=10000)),
        world.store.deployment(DEP),
        world.store.revisions(DEP),
        world.store.approvals(DEP),
        world.store.releases(kind="agent", item=AGENT),
    )


def refused(answer, code: str, status: int = 409) -> str:
    assert answer.status_code == status, answer.text
    error = answer.json()["error"]
    assert error["code"] == code, error
    return error["message"]


def last_event(world: World, type: str):
    found = [e for e in world.store.events(deployment_id=DEP, limit=10000) if e.type == type]
    assert found, f"no {type} event"
    return found[-1]


def types(world: World) -> list[str]:
    return [e.type for e in world.store.events(deployment_id=DEP, limit=10000)]


# -- record a release ----------------------------------------------------------------------


def test_record_a_release_with_the_same_authority():
    world = registry_owned()
    before = world.store.deployment(DEP)
    body = published("a")

    answer = world.post(RELEASES, body)

    assert answer.status_code == 200, answer.text
    got = answer.json()
    assert got["created"] is True
    assert got["release"]["name"] == body["agentRelease"]["metadata"]["name"]
    assert got["release"]["lineage"] == body["lineage"]
    assert got["revision"]["number"] == 3
    assert got["revision"]["origin"] == "registry"
    assert got["revision"]["phase"] == "awaiting-runtime"
    assert got["revision"]["reason"] == WAITING
    assert got["revision"]["approvalId"] is None

    event = last_event(world, "release-recorded")
    assert event.actor == PERSON
    revision = world.store.revision(DEP, 3)
    deployment = world.store.deployment(DEP)
    assert revision.generation == event.seq
    # A candidate without an approval replaces nothing that runs (delivery.md section 4).
    assert deployment.generation == before.generation
    assert deployment.document["agent"] == before.document["agent"]
    assert deployment.current_revision == 2  # nothing is live until it is approved and Ready
    assert revision.authority == canonical.authority_of(body["agent"])
    assert revision.policy_revision == world.store.tenant(CLUSTER, TENANT).policy_revision
    release = world.store.release(revision.release_id)
    assert world.store.requirements(release_id=release.id) == derive.requirements_of(
        release.id, body["agent"]
    )
    assert [r.phase for r in world.store.revisions(DEP)] == [
        "activated",
        "activated",
        "awaiting-runtime",
    ]


def test_record_the_same_release_again():
    world = registry_owned()
    record(world, "a")
    before = snapshot(world)

    answer = world.post(RELEASES, published("a"))

    assert answer.status_code == 200, answer.text
    assert answer.json()["created"] is False
    assert answer.json()["revision"]["number"] == 3
    assert snapshot(world) == before


def test_record_a_different_document_under_the_same_release_name():
    world = registry_owned()
    record(world, "a")
    before = snapshot(world)
    body = published("a")
    body["agentRelease"]["spec"]["image"] = body["agentRelease"]["spec"]["image"][:-1] + "0"

    refused(world.post(RELEASES, body), "conflict")

    assert snapshot(world) == before


def test_record_a_document_that_says_it_is_approved():
    world = registry_owned()
    before = snapshot(world)
    body = published("a")
    body["agentRelease"]["spec"]["approval"] = {
        "approved": True,
        "approvedBy": PERSON,
        "approvedAt": "2026-11-17T12:00:00Z",
    }

    refused(world.post(RELEASES, body), "invalid-arguments", 400)
    assert snapshot(world) == before

    # Saying it is not approved is accepted, and the block is not part of the record.
    body["agentRelease"]["spec"]["approval"] = {"approved": False}
    answer = world.post(RELEASES, body)
    assert answer.status_code == 200, answer.text
    assert "approval" not in answer.json()["release"]["document"]["spec"]


@pytest.mark.parametrize(
    "spoil",
    [
        lambda b: b["agent"].update(kind="Deployment"),
        lambda b: b["agentRelease"].update(apiVersion="agentplatform.io/v2"),
        lambda b: b["agentRelease"]["spec"].update(agent="someone-else"),
        lambda b: b["agent"]["metadata"].update(name="someone-else"),
        lambda b: b["agent"]["metadata"].update(namespace="default"),
        lambda b: b["agentRelease"]["metadata"].update(namespace="default"),
        lambda b: b["agentRelease"]["metadata"].update(name="not a name"),
    ],
    ids=[
        "agent-kind",
        "release-api-version",
        "release-names-another-agent",
        "agent-has-another-name",
        "agent-namespace",
        "release-namespace",
        "release-name",
    ],
)
def test_record_a_release_that_does_not_belong_here(spoil):
    world = registry_owned()
    before = snapshot(world)
    body = published("a")
    spoil(body)

    refused(world.post(RELEASES, body), "invalid-arguments", 400)

    assert snapshot(world) == before


# -- the verdict, approval and activation --------------------------------------------------


def test_verdict_passes_for_the_first_release_under_the_registry():
    world = registry_owned()
    number = record(world, "a")

    assert report(world, number) is True

    revision = world.store.revision(DEP, number)
    assert revision.phase == "pending-approval"
    assert revision.reason == "no release has been approved under the registry yet"
    assert revision.approval_id is None  # an imported approval never carries forward
    assert last_event(world, "approval-needed").actor == "registry"
    assert advance(world) is False  # the same report again changes nothing

    seen = preview(world, number)
    assert seen["approvable"] is True and seen["approved"] is False
    assert seen["gateFailures"] == [] and seen["changes"] == []
    assert seen["observation"]["state"] == "fresh"
    assert seen["release"] == world.store.release(revision.release_id).name
    assert seen["authorityDigest"] == revision.authority_digest
    assert seen["baseAuthorityDigest"] == world.store.revision(DEP, 2).authority_digest
    assert seen["policyRevision"] == world.store.tenant(CLUSTER, TENANT).policy_revision


def test_advance_cluster_reports_what_changed():
    world = registry_owned()
    number = record(world, "a")
    world.tick()
    world.cluster_shows(number)
    limit = world.settings.observation_max_age_seconds

    first = lifecycle.advance_cluster(world.store, CLUSTER, now=world.now, max_age_seconds=limit)
    again = lifecycle.advance_cluster(world.store, CLUSTER, now=world.now, max_age_seconds=limit)

    assert first == [DEP]
    assert again == []


def test_approve_it_and_the_cluster_reports_ready():
    world = registry_owned()
    number = record(world, "a")
    report(world, number)
    recorded_at = world.store.revision(DEP, number).generation
    world.tick()

    answer = approve(world, number, ticket="OPS-1")

    assert answer.status_code == 200, answer.text
    event = last_event(world, "approved")
    assert event.actor == PERSON
    approval = world.store.approval("approval-0001")
    assert answer.json()["approval"]["id"] == approval.id
    assert (approval.origin, approval.state, approval.revision) == ("registry", "active", number)
    assert approval.approved_by == PERSON
    assert approval.approved_at == world.now
    assert approval.ticket == "OPS-1"
    assert approval.evidence["failures"] == []
    assert approval.evidence["signer"].endswith(f"refs/tags/{AGENT}/v0.1.9")
    assert len(approval.evidence["attestations"]) == 5
    revision = world.store.revision(DEP, number)
    assert revision.approval_id == approval.id
    assert (revision.phase, revision.reason) == ("awaiting-runtime", APPROVED_WAITING)
    assert revision.generation == event.seq > recorded_at
    # The approvals of the pull request path are history and stay as they were.
    assert [a.state for a in world.store.approvals(DEP) if a.origin == "imported"] == [
        "active",
        "active",
    ]
    assert world.store.deployment(DEP).current_revision == 2

    # The cluster still has the object as it was before the approval: nothing to conclude.
    assert report(world, number, behind=True) is False
    assert world.store.revision(DEP, number).phase == "awaiting-runtime"

    # It has the approved object, and the controller has not caught up.
    assert report(world, number, ready=False) is True
    assert world.store.revision(DEP, number).reason == "waiting for Ready: NotApproved"
    assert advance(world) is False

    # Ready.
    before = world.store.deployment(DEP).generation
    assert report(world, number) is True
    revision = world.store.revision(DEP, number)
    deployment = world.store.deployment(DEP)
    activated = last_event(world, "revision-activated")
    assert (revision.phase, revision.reason, revision.activated_at) == ("activated", "", world.now)
    assert deployment.current_revision == number
    assert deployment.generation == activated.seq > before
    assert advance(world) is False


def test_the_next_release_with_the_same_authority_needs_no_person():
    world = registry_owned()
    first_release_live(world)
    number = record(world, "b")
    recorded_at = world.store.revision(DEP, number).generation

    assert report(world, number) is True

    revision = world.store.revision(DEP, number)
    carried = last_event(world, "approval-carried-forward")
    assert revision.approval_id == "approval-0001"
    assert (revision.phase, revision.reason) == ("awaiting-runtime", APPROVED_WAITING)
    assert revision.generation == carried.seq > recorded_at

    assert report(world, number) is True
    assert world.store.revision(DEP, number).phase == "activated"
    assert world.store.deployment(DEP).current_revision == number
    assert types(world).count("approved") == 1
    assert [a.id for a in world.store.approvals(DEP) if a.origin == "registry"] == ["approval-0001"]


def test_the_next_release_with_a_lower_budget_is_carried_forward():
    world = registry_owned()
    first = first_release_live(world)
    number = record(world, "b", lower_budget)

    assert report(world, number) is True

    revision = world.store.revision(DEP, number)
    assert revision.authority_digest != world.store.revision(DEP, first).authority_digest
    assert revision.approval_id == "approval-0001"
    assert report(world, number) is True
    assert world.store.deployment(DEP).current_revision == number


def test_the_next_release_with_one_more_tool_waits_for_a_person():
    world = registry_owned()
    first_release_live(world)
    number = record(world, "b", one_more_tool)

    assert report(world, number) is True

    revision = world.store.revision(DEP, number)
    assert revision.phase == "pending-approval"
    assert revision.approval_id is None
    assert "list_tables" in revision.reason
    assert last_event(world, "approval-needed").detail["reason"] == revision.reason
    seen = preview(world, number)
    assert seen["approvable"] is True
    assert seen["changes"] == [revision.reason]

    # A person approves the wider authority; the approval it replaces is no longer in force.
    assert approve(world, number, "approval-0002").status_code == 200
    states = {a.id: a.state for a in world.store.approvals(DEP) if a.origin == "registry"}
    assert states == {"approval-0001": "superseded", "approval-0002": "active"}
    assert report(world, number) is True
    assert world.store.deployment(DEP).current_revision == number


def test_the_next_release_after_the_tenant_policy_changed_waits_for_a_person():
    world = registry_owned()
    first_release_live(world)
    changed = world.change_policy()
    number = record(world, "b")

    assert report(world, number) is True

    revision = world.store.revision(DEP, number)
    assert revision.policy_revision == changed
    assert revision.phase == "pending-approval"
    assert revision.reason == "the tenant policy changed since the last approval"
    assert preview(world, number)["policyRevision"] == changed


def test_a_gate_fails():
    world = registry_owned()
    number = record(world, "a")
    revision = world.store.revision(DEP, number)
    release = world.store.release(revision.release_id)
    status = verdict(release.document)
    status["policyDigest"] = "sha256:" + "0" * 64

    assert report(world, number, status=status) is True

    observation = world.store.observation(CLUSTER, "agentrelease", NAMESPACE, release.name)
    failures = gates.gate_failures(
        release.document, observation.document, derive.pin_for(world.store, CLUSTER, TENANT, AGENT)
    )
    assert failures
    revision = world.store.revision(DEP, number)
    assert (revision.phase, revision.reason) == ("awaiting-runtime", failures[0])
    assert last_event(world, "runtime-verdict").detail["failures"] == failures
    assert advance(world) is False

    seen = preview(world, number)
    assert seen["approvable"] is False
    assert seen["gateFailures"] == failures
    before = snapshot(world)
    message = refused(approve(world, number), "refused")
    assert all(failure in message for failure in failures)
    assert snapshot(world) == before

    # The controller's verdict changes; the registry follows.
    assert report(world, number) is True
    assert world.store.revision(DEP, number).phase == "pending-approval"


@pytest.mark.parametrize(
    ("how", "failure"),
    [
        ("missing", "the release has not been observed on the cluster"),
        ("old", "the last observation of the release is too old"),
        ("behind", "the cluster does not have this revision yet"),
    ],
)
def test_an_observation_that_cannot_be_used(how, failure):
    world = registry_owned()
    number = record(world, "a")
    if how == "old":
        world.cluster_shows(number)
        world.tick(world.settings.observation_max_age_seconds + 1)
    elif how == "behind":
        world.tick()
        world.cluster_shows(number, generation=world.store.revision(DEP, number).generation - 1)
    before = snapshot(world)

    assert advance(world) is False
    seen = preview(world, number)
    assert seen["approvable"] is False
    assert seen["gateFailures"] == [failure]
    refused(approve(world, number), "refused")

    assert snapshot(world) == before
    assert world.store.revision(DEP, number).reason == WAITING


@pytest.mark.parametrize("field", ["authorityDigest", "baseAuthorityDigest", "policyRevision"])
def test_approve_with_a_binding_that_is_no_longer_so(field):
    world = registry_owned()
    number = record(world, "a")
    report(world, number)
    before = snapshot(world)

    answer = approve(world, number, **{field: "sha256:" + "0" * 64})

    refused(answer, "stale-approval")
    assert snapshot(world) == before


def test_approve_an_older_revision():
    world = registry_owned()
    older = record(world, "a")
    report(world, older)
    seen = preview(world, older)
    newer = record(world, "b")
    report(world, newer)
    before = snapshot(world)

    answer = world.post(
        APPROVALS,
        {
            "approvalId": "approval-0001",
            "revision": older,
            "authorityDigest": seen["authorityDigest"],
            "baseAuthorityDigest": seen["baseAuthorityDigest"],
            "policyRevision": seen["policyRevision"],
        },
    )

    refused(answer, "stale-approval")
    assert snapshot(world) == before
    missing = world.post(
        APPROVALS,
        {
            "approvalId": "approval-0002",
            "revision": 9,
            "authorityDigest": seen["authorityDigest"],
            "baseAuthorityDigest": seen["baseAuthorityDigest"],
            "policyRevision": seen["policyRevision"],
        },
    )
    refused(missing, "not-found", 404)
    assert snapshot(world) == before


def test_approve_twice_with_the_same_id():
    world = registry_owned()
    number = record(world, "a")
    report(world, number)
    assert approve(world, number).status_code == 200
    before = snapshot(world)

    refused(approve(world, number), "replayed-approval")

    assert snapshot(world) == before


def test_approve_an_already_approved_revision_with_a_new_id():
    world = registry_owned()
    number = record(world, "a")
    report(world, number)
    assert approve(world, number).status_code == 200
    assert preview(world, number)["approved"] is True
    assert preview(world, number)["approvable"] is False
    before = snapshot(world)

    refused(approve(world, number, "approval-0002"), "stale-approval")

    assert snapshot(world) == before


def test_a_newer_release_recorded_while_one_waits():
    world = registry_owned()
    waiting_for_a_person = record(world, "a")
    report(world, waiting_for_a_person)
    assert world.store.revision(DEP, waiting_for_a_person).phase == "pending-approval"

    waiting_for_the_cluster = record(world, "b")
    assert world.store.revision(DEP, waiting_for_a_person).phase == "superseded"

    newest = record(world, "c")
    assert world.store.revision(DEP, waiting_for_the_cluster).phase == "superseded"

    # Reports about the older ones change nothing; the newest moves on.
    world.tick()
    world.cluster_shows(waiting_for_a_person)
    world.cluster_shows(waiting_for_the_cluster)
    assert advance(world) is False
    assert report(world, newest) is True
    assert [r.phase for r in world.store.revisions(DEP)] == [
        "activated",
        "activated",
        "superseded",
        "superseded",
        "pending-approval",
    ]


# -- pin, enable, disable ------------------------------------------------------------------


def pinned_with_a_later_release_live(world: World) -> int:
    answer = world.post(PIN, {"revision": 2})
    assert answer.status_code == 200, answer.text
    assert answer.json()["changed"] is True
    return first_release_live(world)


def test_pin_then_record_and_approve_a_release():
    world = registry_owned()
    before = world.store.deployment(DEP)

    answer = world.post(PIN, {"revision": 2})

    assert answer.status_code == 200, answer.text
    assert answer.json()["changed"] is True
    deployment = world.store.deployment(DEP)
    assert (deployment.pinned_revision, deployment.current_revision) == (2, 2)
    assert deployment.generation == last_event(world, "pinned").seq > before.generation
    unchanged = snapshot(world)
    again = world.post(PIN, {"revision": 2})
    assert again.status_code == 200 and again.json()["changed"] is False
    assert snapshot(world) == unchanged

    number = first_release_live(world)

    deployment = world.store.deployment(DEP)
    assert world.store.revision(DEP, number).phase == "activated"
    assert (deployment.pinned_revision, deployment.current_revision) == (2, 2)


def test_pin_to_a_revision_that_cannot_hold_the_pointer():
    world = registry_owned()
    number = record(world, "a")
    before = snapshot(world)

    refused(world.post(PIN, {"revision": 9}), "not-found", 404)
    refused(world.post(PIN, {"revision": number}), "refused")  # not activated
    world.tick(world.settings.observation_max_age_seconds + 1)
    refused(world.post(PIN, {"revision": 1}), "refused")  # nothing recent says it is Ready

    assert snapshot(world) == before


def test_unpin():
    world = registry_owned()
    number = pinned_with_a_later_release_live(world)
    before = world.store.deployment(DEP)

    answer = world.post(PIN, {"revision": None})

    assert answer.status_code == 200, answer.text
    assert answer.json()["changed"] is True
    deployment = world.store.deployment(DEP)
    assert (deployment.pinned_revision, deployment.current_revision) == (None, number)
    assert deployment.generation == last_event(world, "unpinned").seq > before.generation
    unchanged = snapshot(world)
    again = world.post(PIN, {"revision": None})
    assert again.status_code == 200 and again.json()["changed"] is False
    assert snapshot(world) == unchanged


def test_disable_and_enable():
    world = registry_owned()
    before = world.store.deployment(DEP)

    answer = world.post(ENABLED, {"enabled": False, "reason": "maintenance"})

    assert answer.status_code == 200, answer.text
    assert answer.json()["changed"] is True
    assert answer.json()["deployment"]["enabled"] is False
    disabled = last_event(world, "disabled")
    deployment = world.store.deployment(DEP)
    assert disabled.detail["reason"] == "maintenance" and disabled.actor == PERSON
    assert deployment.enabled is False
    assert deployment.generation == disabled.seq > before.generation
    unchanged = snapshot(world)
    again = world.post(ENABLED, {"enabled": False, "reason": "again"})
    assert again.status_code == 200 and again.json()["changed"] is False
    assert snapshot(world) == unchanged

    answer = world.post(ENABLED, {"enabled": True})

    assert answer.json()["changed"] is True
    deployment = world.store.deployment(DEP)
    assert deployment.enabled is True
    assert deployment.generation == last_event(world, "enabled").seq > disabled.seq


# -- who may write, and when ---------------------------------------------------------------

BINDING = {
    "approvalId": "approval-0001",
    "revision": 2,
    "authorityDigest": "sha256:" + "0" * 64,
    "baseAuthorityDigest": "sha256:" + "0" * 64,
    "policyRevision": "sha256:" + "0" * 64,
}


def every_write(world: World, deployment: str = DEP, **how):
    """One call to each route of the write router, with a body that is well formed."""
    base = f"/v1/deployments/{deployment}"
    return {
        "releases": world.post(f"{base}/releases", published("a"), **how),
        "preview": world.get(f"{base}/revisions/2/approval", **how),
        "approvals": world.post(f"{base}/approvals", copy.deepcopy(BINDING), **how),
        "enabled": world.post(f"{base}/enabled", {"enabled": False, "reason": "x"}, **how),
        "pin": world.post(f"{base}/pin", {"revision": 1}, **how),
    }


def test_any_write_for_an_agent_on_the_pull_request_path():
    world = World()
    world.seed()
    world.cluster_shows_everything()
    before = snapshot(world)

    for route, answer in every_write(world).items():
        message = refused(answer, "wrong-path")
        assert "just agent authority take db-admin" in message, route

    assert snapshot(world) == before
    assert advance(world) is False


def test_any_write_for_a_deployment_that_is_not_an_agent_here():
    world = registry_owned()
    before = snapshot(world)

    for answer in every_write(world, "agent:kubani:tenant-test:nobody").values():
        refused(answer, "not-found", 404)

    assert snapshot(world) == before


def test_any_write_while_in_recovery():
    world = registry_owned()
    number = record(world, "a")
    world.tick()
    world.cluster_shows(number)
    modes.enter_recovery(world.store, "a test", actor="test")
    before = snapshot(world)

    for answer in every_write(world).values():
        message = refused(answer, "in-recovery")
        assert answer.json()["error"]["retryable"] is True
        assert "a test" in message

    assert advance(world) is False
    assert snapshot(world) == before


@pytest.mark.parametrize("token", ["reader", "robot", "nobody"])
def test_a_caller_who_may_not_write(token):
    world = registry_owned()
    before = snapshot(world)

    for answer in every_write(world, token=token).values():
        refused(answer, "refused", 403)

    assert snapshot(world) == before


def test_a_caller_who_is_not_signed_in():
    world = registry_owned()
    before = snapshot(world)

    for token in (None, "not-a-token"):
        for answer in every_write(world, token=token).values():
            refused(answer, "not-signed-in", 401)

    assert snapshot(world) == before
