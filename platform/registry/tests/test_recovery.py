"""Recovery: `contract/projection.md` section 8, through the HTTP application."""

from __future__ import annotations

import copy

import pytest

from agent_registry import canonical, modes
from agent_registry.config import ANNOTATION_PREFIX
from agent_registry.models import Observation
from tests.test_write_cases import APPROVALS, ENABLED, PIN, RELEASES, record, refused, report
from tests.test_write_races import race
from tests.world import CLUSTER, DEP, NAMESPACE, World, published, registry_owned

RECOVERY = "/v1/recovery"
LEAVE = "/v1/recovery/leave"
OTHER = "db-admin-2026-11-19-eeeeeeee"
APPROVED = {
    "approved": True,
    "approvedBy": "x@example.com",
    "approvedAt": "2026-11-18T08:00:00Z",
    "ticket": "OPS-9",
}


def heard(world: World, last_generation: int = 0) -> None:
    """The provisioner's `report` observation, as `rules.intake.accept` stores it."""
    world.store.put_observations(
        CLUSTER,
        [
            Observation(
                cluster=CLUSTER,
                subject_kind="projection",
                namespace="",
                name="report",
                observed_at=world.now,
                document={"lastGeneration": last_generation, "objects": 3},
            )
        ],
    )


def recovering(world: World | None = None, *, report_after: bool = True) -> World:
    """In recovery, with the cluster showing exactly what the registry projects."""
    world = world or registry_owned()
    modes.enter_recovery(world.store, "a test", actor="test")
    world.tick()
    world.cluster_shows_everything()
    if report_after:
        heard(world)
    return world


def edit(world: World, kind: str, name: str, change) -> None:
    found = world.store.observation(CLUSTER, kind, NAMESPACE, name)
    document = copy.deepcopy(found.document)
    change(document)
    world.store.put_observations(CLUSTER, [found.model_copy(update={"document": document})])


def release_name(world: World, number: int) -> str:
    return world.store.release(world.store.revision(DEP, number).release_id).name


def show(world: World) -> dict:
    answer = world.get(RECOVERY)
    assert answer.status_code == 200, answer.text
    return answer.json()


def found(world: World) -> list[tuple[str, str]]:
    return [(d["subject"], d["field"]) for d in show(world)["differences"]]


def annotate(key: str, value: str):
    return lambda d: d["metadata"]["annotations"].update({ANNOTATION_PREFIX + key: value})


def other_release(world: World, block: dict) -> None:
    source = world.store.observation(CLUSTER, "agentrelease", NAMESPACE, release_name(world, 2))
    document = copy.deepcopy(source.document)
    document["metadata"]["name"] = OTHER
    document["spec"]["approval"] = block
    world.store.put_observations(
        CLUSTER, [source.model_copy(update={"name": OTHER, "document": document})]
    )


def counts(world: World) -> tuple:
    return (world.store.generation(), len(world.store.events(limit=10000)))


# -- the report ----------------------------------------------------------------------------


def test_in_normal_mode_there_are_no_differences():
    world = registry_owned()
    edit(world, "agent", "db-admin", lambda d: d["spec"].update(enabled=False))

    body = show(world)

    assert body["mode"] == "normal" and body["complete"] is True
    assert body["differences"] == [] and body["waitingFor"] == []
    assert body["digest"] == canonical.digest([])


def test_the_report_is_not_complete_until_the_cluster_has_reported_since_recovery_began():
    world = registry_owned()
    modes.enter_recovery(world.store, "a test", actor="test")
    assert show(world)["complete"] is False
    assert show(world)["waitingFor"] == [CLUSTER]

    world.tick()
    world.cluster_shows_everything()
    assert show(world)["waitingFor"] == [CLUSTER]
    world.store.put_observations(
        CLUSTER,
        [
            Observation(
                cluster=CLUSTER,
                subject_kind="projection",
                namespace="",
                name="report",
                observed_at=world.store.meta().mode_changed_at.replace(year=2020),
                document={"lastGeneration": 0, "objects": 3},
            )
        ],
    )
    assert show(world)["waitingFor"] == [CLUSTER]

    heard(world)
    body = show(world)
    assert body["mode"] == "recovery" and body["reason"] == "a test"
    assert body["complete"] is True and body["waitingFor"] == []
    assert body["differences"] == []


def test_a_report_that_is_too_old_is_not_complete():
    world = recovering()

    world.tick(world.settings.observation_max_age_seconds + 1)

    assert show(world)["waitingFor"] == [CLUSTER]


def difference_cases():
    return [
        pytest.param(
            lambda w: edit(w, "agent", "db-admin", lambda d: d["spec"]["runtime"].update(cpu="4")),
            [("agent/db-admin", "agent")],
            id="agent",
        ),
        pytest.param(
            lambda w: edit(w, "agent", "db-admin", lambda d: d["spec"].update(enabled=False)),
            [("agent/db-admin", "enabled")],
            id="enabled",
        ),
        pytest.param(
            lambda w: edit(w, "agent", "db-admin", annotate("current-release", release_name(w, 1))),
            [("agent/db-admin", "current-release")],
            id="current-release",
        ),
        pytest.param(
            lambda w: other_release(w, {"approved": False}),
            [(f"agentrelease/{OTHER}", "release")],
            id="release",
        ),
        pytest.param(
            lambda w: edit(
                w,
                "agentrelease",
                release_name(w, 1),
                lambda d: d["spec"]["approval"].update(approvedBy="y@example.com"),
            ),
            lambda w: [(f"agentrelease/{release_name(w, 1)}", "approval")],
            id="approval-differs",
        ),
        pytest.param(
            lambda w: edit(w, "agent", "db-admin", annotate("generation", "99")),
            [("agent/db-admin", "generation")],
            id="generation-agent",
        ),
        pytest.param(
            lambda w: edit(w, "agentrelease", release_name(w, 2), annotate("generation", "99")),
            lambda w: [(f"agentrelease/{release_name(w, 2)}", "generation")],
            id="generation-release",
        ),
    ]


@pytest.mark.parametrize(("spoil", "expected"), difference_cases())
def test_each_difference_of_the_report(spoil, expected):
    world = recovering()
    assert found(world) == []

    spoil(world)

    assert found(world) == (expected(world) if callable(expected) else expected)


def test_an_approval_the_cluster_shows_and_the_revision_lacks_is_a_difference():
    world = registry_owned()
    number = record(world, "a")
    recovering(world)
    assert found(world) == []

    edit(
        world,
        "agentrelease",
        release_name(world, number),
        lambda d: d["spec"].update(approval=APPROVED),
    )

    assert found(world) == [(f"agentrelease/{release_name(world, number)}", "approval")]
    (difference,) = show(world)["differences"]
    assert difference["registry"] == {"approved": False} and difference["cluster"] == APPROVED


def test_a_registry_approval_the_cluster_does_not_show_yet_is_not_a_difference():
    world = registry_owned()
    number = record(world, "a")
    report(world, number)
    assert world.post(APPROVALS, approval_body(world, number)).status_code == 200
    modes.enter_recovery(world.store, "a test", actor="test")
    world.tick()
    world.cluster_shows_everything()
    world.cluster_shows(number, behind=True)
    heard(world)

    assert [d for d in found(world) if d[1] == "approval"] == []


def approval_body(world: World, number: int) -> dict:
    seen = world.get(f"/v1/deployments/{DEP}/revisions/{number}/approval").json()
    return {
        "approvalId": "approval-0001",
        "revision": number,
        "authorityDigest": seen["authorityDigest"],
        "baseAuthorityDigest": seen["baseAuthorityDigest"],
        "policyRevision": seen["policyRevision"],
    }


def test_differences_are_sorted_and_the_digest_follows_them():
    world = recovering()
    edit(world, "agent", "db-admin", lambda d: d["spec"].update(enabled=False))
    edit(world, "agent", "db-admin", lambda d: d["spec"]["runtime"].update(cpu="4"))
    other_release(world, {"approved": False})
    first = show(world)

    assert [(d["subject"], d["field"]) for d in first["differences"]] == [
        ("agent/db-admin", "agent"),
        ("agent/db-admin", "enabled"),
        (f"agentrelease/{OTHER}", "release"),
    ]
    assert first["digest"] == canonical.digest(first["differences"])
    assert show(world)["digest"] == first["digest"]
    edit(world, "agent", "db-admin", lambda d: d["spec"].update(enabled=True))
    assert show(world)["digest"] != first["digest"]


# -- leaving -------------------------------------------------------------------------------


def test_leave_is_refused_for_a_wrong_digest_and_writes_nothing():
    world = recovering()
    before = counts(world)

    message = refused(world.post(LEAVE, {"digest": "0" * 64}), "conflict")

    assert "digest" in message or "differences" in message
    assert counts(world) == before and world.store.meta().mode == "recovery"


def test_leave_is_refused_while_the_report_is_incomplete():
    world = recovering(report_after=False)
    before = counts(world)

    message = refused(world.post(LEAVE, {"digest": show(world)["digest"]}), "conflict")

    assert CLUSTER in message
    assert counts(world) == before and world.store.meta().mode == "recovery"


def test_leave_is_refused_when_the_registry_is_not_in_recovery():
    world = registry_owned()
    before = counts(world)

    refused(world.post(LEAVE, {"digest": canonical.digest([])}), "conflict")

    assert counts(world) == before


def test_leave_without_differences_still_numbers_everything_above_the_cluster():
    world = recovering()
    heard(world, last_generation=40)
    generation = world.store.generation()

    answer = world.post(LEAVE, {"digest": show(world)["digest"]})

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["mode"] == "normal" and body["adopted"] == []
    assert body["generation"] == world.store.generation() > 40 > generation
    assert world.store.meta().mode == "normal"
    seq = body["generation"]
    assert world.store.deployment(DEP).generation == seq
    assert {r.generation for r in world.store.revisions(DEP)} == {seq}


def test_leave_makes_the_registry_equal_to_the_cluster():
    world = registry_owned()
    number = record(world, "a")
    recovering(world)
    edit(
        world,
        "agent",
        "db-admin",
        lambda d: (d["spec"]["runtime"].update(cpu="4"), d["spec"].update(enabled=False)),
    )
    other_release(world, APPROVED)
    edit(
        world,
        "agentrelease",
        release_name(world, number),
        lambda d: d["spec"].update(approval=APPROVED),
    )
    edit(world, "agent", "db-admin", annotate("current-release", OTHER))
    edit(world, "agent", "db-admin", annotate("generation", "50"))
    heard(world, last_generation=70)
    before = show(world)

    answer = world.post(LEAVE, {"digest": before["digest"]})

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["mode"] == "normal"
    assert [(d["subject"], d["field"]) for d in body["adopted"]] == [
        (d["subject"], d["field"]) for d in before["differences"]
    ]
    assert {d["field"] for d in body["adopted"]} == {
        "agent",
        "enabled",
        "current-release",
        "release",
        "approval",
        "generation",
    }
    seq = [e for e in world.store.events(limit=10000) if e.type == "recovery-left"][-1]
    assert seq.detail == {"digest": before["digest"], "adopted": len(before["differences"])}
    assert body["generation"] == world.store.generation() == seq.seq > 70
    assert world.store.meta().mode == "normal"

    deployment = world.store.deployment(DEP)
    assert deployment.enabled is False
    assert deployment.document["agent"]["spec"]["runtime"]["cpu"] == "4"
    assert deployment.generation == seq.seq
    revisions = world.store.revisions(DEP)
    assert [r.number for r in revisions] == [1, 2, 3, 4]
    assert {r.generation for r in revisions} == {seq.seq}
    assert deployment.current_revision == 4

    approved = world.store.revision(DEP, number)
    assert approved.phase == "activated" and approved.approval_id == f"recovered:{DEP}@3"
    created = revisions[3]
    assert (created.origin, created.phase, created.approval_id) == (
        "recovered",
        "activated",
        f"recovered:{DEP}@4",
    )
    assert world.store.release(created.release_id).name == OTHER
    assert world.store.requirements(created.release_id)
    assert world.store.resolutions(created.id)
    approval = world.store.approval(f"recovered:{DEP}@4")
    assert (approval.origin, approval.approved_by, approval.ticket, approval.state) == (
        "recovered",
        "x@example.com",
        "OPS-9",
        "active",
    )
    assert approval.evidence == {"approval": APPROVED}
    assert approval.approved_at.isoformat() == "2026-11-18T08:00:00+00:00"

    world.tick()
    modes.enter_recovery(world.store, "a second look", actor="test")
    world.tick()
    heard(world)
    assert found(world) == []


def test_a_recovered_release_that_is_not_approved_awaits_the_runtime():
    world = recovering()
    other_release(world, {"approved": False})
    digest = show(world)["digest"]

    assert world.post(LEAVE, {"digest": digest}).status_code == 200

    created = world.store.revisions(DEP)[-1]
    assert (created.origin, created.phase, created.approval_id) == (
        "recovered",
        "awaiting-runtime",
        None,
    )
    assert created.reason == "recovered unapproved"
    assert world.store.deployment(DEP).current_revision == 2


def test_an_approval_that_is_replaced_becomes_superseded():
    world = recovering()
    first = release_name(world, 1)
    edit(
        world,
        "agentrelease",
        first,
        lambda d: d["spec"]["approval"].update(approvedBy="y@example.com"),
    )
    old = world.store.revision(DEP, 1).approval_id

    assert world.post(LEAVE, {"digest": show(world)["digest"]}).status_code == 200

    revision = world.store.revision(DEP, 1)
    assert revision.approval_id == f"recovered:{DEP}@1" != old
    assert world.store.approval(old).state == "superseded"
    assert world.store.approval(revision.approval_id).approved_by == "y@example.com"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (RELEASES, published("a")),
        (APPROVALS, {"approvalId": "approval-0001", "revision": 2}),
        (ENABLED, {"enabled": False}),
        (PIN, {"revision": 1}),
        (f"/v1/deployments/{DEP}/authority/take", None),
        (f"/v1/deployments/{DEP}/authority/return", None),
    ],
)
def test_any_write_route_in_recovery_is_refused(path, body):
    world = recovering()
    before = counts(world)

    refused(world.post(path, body), "in-recovery")

    assert counts(world) == before


def test_leave_races_a_change_to_what_the_cluster_holds():
    world = recovering()
    seen = show(world)
    assert seen["differences"] == []

    race(world, lambda: other_release(world, {"approved": False}))
    before = counts(world)

    message = refused(world.post(LEAVE, {"digest": seen["digest"]}), "conflict")

    assert "differences" in message
    assert counts(world) == before
    assert world.store.meta().mode == "recovery"
    assert len(world.store.revisions(DEP)) == 2
