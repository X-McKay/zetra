"""The table of `contract/delivery.md` section 5 (D1 to D15), evaluations (section 6),
rollback (section 7), a new revision of one release (section 5.1) and what status says
(section 10). Through the HTTP application, with the cluster's side played by `tests/world.py`."""

from __future__ import annotations

from agent_registry import canonical
from agent_registry.models import Alias, Deployment, Item, Revocation
from agent_registry.rules import projection, revisions
from tests.test_write_cases import (
    APPROVED_WAITING,
    PIN,
    RELEASES,
    advance,
    approve,
    first_release_live,
    last_event,
    lower_budget,
    record,
    refused,
    report,
    types,
)
from tests.world import (
    AGENT,
    CLUSTER,
    DEP,
    PERSON,
    SUITE,
    World,
    published,
    registry_owned,
    verdict,
)

ROLLBACK = f"/v1/deployments/{DEP}/rollback"
CLAIM = f"/v1/clusters/{CLUSTER}/evaluations/claim"
NO_SUITE = "the release carries no evaluation suite and the current release was admitted by one"


def result_path(number: int, attempt: int = 1) -> str:
    return f"/v1/deployments/{DEP}/revisions/{number}/evaluations/{attempt}/result"


# -- helpers -------------------------------------------------------------------------------


def record_with_suite(world: World, letter: str, change=None) -> int:
    answer = world.post(RELEASES, published(letter, change, suite=SUITE))
    assert answer.status_code == 200, answer.text
    return answer.json()["revision"]["number"]


def drive(world: World, number: int) -> None:
    """The cluster reports, a person approves when the registry asks, the cluster reports Ready."""
    report(world, number)
    if world.store.revision(DEP, number).phase == "pending-approval":
        assert approve(world, number, f"approval-{number:04d}").status_code == 200
    report(world, number)


def claim(world: World, name: str = "ev-1", token: str = "evaluator", lease: int = 900):
    return world.post(CLAIM, {"evaluator": name, "leaseSeconds": lease}, token=token)


def send(
    world: World,
    number: int,
    verdict_: str,
    *,
    attempt: int = 1,
    name: str = "ev-1",
    summary: str = "1 of 2 cases passed; acceptance needs all",
):
    body = {
        "evaluator": name,
        "evaluatorVersion": "0.1.0",
        "verdict": verdict_,
        "summary": summary,
        "cases": [{"id": "summary", "passed": verdict_ == "passed", "sessionId": "s-1"}],
    }
    return world.post(result_path(number, attempt), body, token="evaluator")


def evaluated(world: World, number: int, verdict_: str, **how):
    """Claim the revision's open evaluation and report its verdict."""
    assert claim(world, how.get("name", "ev-1")).json()["evaluation"] is not None
    answer = send(world, number, verdict_, **how)
    assert answer.status_code == 200, answer.text
    return answer.json()


def evaluating(world: World, letter: str = "a", change=None) -> int:
    number = record_with_suite(world, letter, change)
    drive(world, number)
    assert world.store.revision(DEP, number).phase == "evaluating"
    return number


def revoke(world: World, name: str, rid: str = "revocation-1") -> None:
    with world.store.transaction("test") as tx:
        tx.add_revocation(
            Revocation(
                id=rid,
                kind="agent",
                item=AGENT,
                release=name,
                reason="security",
                sessions="cancel",
                summary="test",
                revoked_by="al",
                revoked_at=world.now,
                indexed_at=world.now,
            )
        )
        tx.event("release-revoked", detail={"id": rid})


def release_name(world: World, number: int) -> str:
    return world.store.release(world.store.revision(DEP, number).release_id).name


def agent_in_force(world: World) -> dict:
    return world.store.deployment(DEP).document["agent"]


def agent_of(world: World, number: int) -> dict:
    return world.store.revision(DEP, number).agent_document


def phases(world: World) -> list[str]:
    return [r.phase for r in world.store.revisions(DEP)]


def status_of(world: World) -> dict:
    answer = world.get("/v1/status", cluster=CLUSTER)
    assert answer.status_code == 200, answer.text
    (found,) = [a for a in answer.json()["agents"] if a["deployment"] == DEP]
    return found


# -- the table -----------------------------------------------------------------------------


def test_d01_a_failing_gate_keeps_the_revision_waiting_with_its_first_failure():
    world = registry_owned()
    number = record(world, "a")
    world.tick()
    world.cluster_shows(number, status={})

    assert advance(world) is True

    revision = world.store.revision(DEP, number)
    assert revision.phase == "awaiting-runtime"
    assert revision.reason == "controller has not verified the signature"
    assert advance(world) is False
    assert types(world).count("runtime-verdict") == 1


def test_d02_a_covered_release_has_its_approval_carried_forward_and_is_in_force():
    world = registry_owned()
    first_release_live(world)
    number = record(world, "b", lower_budget)
    assert agent_in_force(world) == agent_of(world, 3)  # the unapproved candidate changes nothing

    assert report(world, number) is True

    revision = world.store.revision(DEP, number)
    assert revision.approval_id == "approval-0001"
    assert (revision.phase, revision.reason) == ("awaiting-runtime", APPROVED_WAITING)
    assert agent_in_force(world) == agent_of(world, number) != agent_of(world, 3)
    assert (
        world.store.deployment(DEP).generation == last_event(world, "approval-carried-forward").seq
    )


def test_d03_an_uncovered_release_waits_for_a_person_and_changes_nothing_that_runs():
    world = registry_owned()
    number = record(world, "a", lower_budget)
    before = world.store.deployment(DEP)

    assert report(world, number) is True

    revision = world.store.revision(DEP, number)
    assert revision.phase == "pending-approval"
    assert last_event(world, "approval-needed").detail["reason"] == revision.reason
    assert world.store.deployment(DEP) == before


def test_d04_an_approval_puts_the_candidate_in_force():
    world = registry_owned()
    number = record(world, "a", lower_budget)
    report(world, number)
    assert agent_in_force(world) != agent_of(world, number)

    assert approve(world, number).status_code == 200

    revision = world.store.revision(DEP, number)
    assert (revision.phase, revision.reason) == ("awaiting-runtime", APPROVED_WAITING)
    assert agent_in_force(world) == agent_of(world, number)
    assert world.store.deployment(DEP).generation == last_event(world, "approved").seq


def test_d05_an_approved_release_that_is_not_ready_waits():
    world = registry_owned()
    number = record(world, "a")
    report(world, number)
    approve(world, number)

    assert report(world, number, ready=False) is True

    revision = world.store.revision(DEP, number)
    assert (revision.phase, revision.reason) == (
        "awaiting-runtime",
        "waiting for Ready: NotApproved",
    )
    assert world.store.deployment(DEP).current_revision == 2


def test_d06_an_approved_ready_release_without_a_suite_is_activated():
    world = registry_owned()

    number = first_release_live(world)

    assert world.store.revision(DEP, number).phase == "activated"
    assert world.store.deployment(DEP).current_revision == number
    assert last_event(world, "revision-activated").detail == {"revision": number, "pinned": False}


def test_d07_a_release_without_a_suite_is_refused_when_the_current_one_was_evaluated():
    world = registry_owned()
    number = evaluating(world, "a", lower_budget)
    evaluated(world, number, "passed")
    assert world.store.deployment(DEP).current_revision == number
    later = record(world, "b")

    drive(world, later)

    revision = world.store.revision(DEP, later)
    assert (revision.phase, revision.reason) == ("refused", NO_SUITE)
    assert world.store.deployment(DEP).current_revision == number
    assert last_event(world, "revision-refused").detail["revision"] == later
    assert agent_in_force(world) == agent_of(world, number)  # settled back to the current one


def test_d08_an_approved_ready_release_with_a_suite_is_evaluated_first():
    world = registry_owned()

    number = evaluating(world)

    revision = world.store.revision(DEP, number)
    assert revision.reason == "waiting for evaluation"
    (attempt,) = world.store.evaluations(DEP)
    assert (attempt.id, attempt.state, attempt.attempt) == (f"{DEP}@{number}#1", "pending", 1)
    assert attempt.suite == SUITE and attempt.suite_digest == canonical.digest(SUITE)
    assert last_event(world, "evaluation-requested").detail["evaluation"] == attempt.id
    assert world.store.deployment(DEP).current_revision == 2


def test_d09_a_passing_evaluation_activates_the_revision():
    world = registry_owned()
    number = evaluating(world)

    answer = evaluated(world, number, "passed")

    assert answer == {"recorded": True, "phase": "activated"}
    assert world.store.deployment(DEP).current_revision == number
    (attempt,) = world.store.evaluations(DEP)
    assert attempt.state == "passed" and attempt.result["acceptance"] == {"min_passed": "all"}
    assert last_event(world, "evaluation-recorded").detail["verdict"] == "passed"
    assert last_event(world, "revision-activated").detail["evaluation"] == attempt.id
    assert status_of(world)["current"]["admittedBy"] == attempt.id


def test_d10_a_failing_evaluation_refuses_the_revision_and_the_current_one_stays():
    world = registry_owned()
    number = evaluating(world, "a", lower_budget)
    assert agent_in_force(world) == agent_of(world, number)

    answer = evaluated(world, number, "failed", summary="1 of 2 cases passed")

    assert answer == {"recorded": True, "phase": "refused"}
    revision = world.store.revision(DEP, number)
    assert revision.reason == "evaluation failed: 1 of 2 cases passed"
    assert world.store.deployment(DEP).current_revision == 2
    assert agent_in_force(world) == agent_of(world, 2)
    shown = status_of(world)
    assert shown["current"]["revision"] == 2
    assert shown["lastRefused"]["revision"] == number
    assert shown["lastRefused"]["evaluation"] == {
        "verdict": "failed",
        "summary": "1 of 2 cases passed",
    }
    assert shown["candidate"] is None


def test_d11_an_error_asks_for_the_next_attempt():
    world = registry_owned()
    number = evaluating(world)

    answer = evaluated(world, number, "error", summary="the session interface refused")

    assert answer == {"recorded": True, "phase": "evaluating"}
    first, second = world.store.evaluations(DEP)
    assert (first.state, second.state, second.attempt) == ("error", "pending", 2)
    assert last_event(world, "evaluation-requested").detail["attempt"] == 2


def test_d12_the_third_error_refuses_the_revision():
    world = registry_owned()
    number = evaluating(world)

    evaluated(world, number, "error", attempt=1)
    evaluated(world, number, "error", attempt=2)
    answer = evaluated(world, number, "error", attempt=3, summary="cannot reach it")

    assert answer == {"recorded": True, "phase": "refused"}
    revision = world.store.revision(DEP, number)
    assert revision.reason == "evaluation could not be completed: cannot reach it"
    assert [e.state for e in world.store.evaluations(DEP)] == ["error"] * 3


def test_d13_a_newer_release_supersedes_the_revision_and_cancels_its_evaluations():
    world = registry_owned()
    number = evaluating(world)
    assert claim(world).json()["evaluation"] is not None  # claimed, not yet answered

    newer = record(world, "b")

    assert world.store.revision(DEP, number).phase == "superseded"
    (attempt,) = world.store.evaluations(DEP)
    assert attempt.state == "cancelled" and attempt.finished_at == world.now
    assert world.store.revision(DEP, newer).phase == "awaiting-runtime"
    # An evaluator that answers later is told nothing was written.
    answer = send(world, number, "passed")
    assert answer.status_code == 200
    assert answer.json() == {"recorded": False, "phase": "superseded"}


def test_d14_an_operator_can_ask_for_another_evaluation_of_a_refused_revision():
    world = registry_owned()
    number = evaluating(world)
    evaluated(world, number, "failed")
    path = f"/v1/deployments/{DEP}/revisions/{number}/evaluations"

    answer = world.post(path, {"reason": "the database was busy"})

    assert answer.status_code == 200, answer.text
    assert answer.json()["revision"]["phase"] == "evaluating"
    assert answer.json()["evaluation"]["attempt"] == 2
    event = last_event(world, "evaluation-requested")
    assert (event.actor, event.detail["reason"]) == (PERSON, "the database was busy")
    # Not refused any more, so a second request is refused.
    refused(world.post(path, {"reason": "again"}), "refused")
    # Nor does a caller who is not an operator get one.
    assert world.post(path, {"reason": "x"}, token="evaluator").status_code == 403


def test_d15_a_pin_holds_the_pointer_when_the_revision_is_activated():
    world = registry_owned()
    assert world.post(PIN, {"revision": 2}).status_code == 200
    number = evaluating(world)

    assert evaluated(world, number, "passed")["phase"] == "activated"

    assert world.store.deployment(DEP).current_revision == 2
    assert last_event(world, "revision-activated").detail["pinned"] is True
    assert status_of(world)["pinned"] == 2


# -- evaluations ---------------------------------------------------------------------------


def test_two_claims_get_different_evaluations_or_null():
    world = registry_owned()
    number = evaluating(world)

    first = claim(world, "ev-1").json()["evaluation"]
    second = claim(world, "ev-2").json()["evaluation"]

    assert second is None
    assert (first["deployment"], first["revision"], first["attempt"]) == (DEP, number, 1)
    assert first["release"] == release_name(world, number)
    assert (first["agent"], first["tenant"]) == (AGENT, "tenant-test")
    assert first["suite"] == SUITE and first["suiteDigest"] == canonical.digest(SUITE)
    assert first["claimExpiresAt"].startswith("2026-11-17T12:15")
    # When the lease has run out the evaluation is offered again, and the first holder loses it.
    world.tick(901)
    again = claim(world, "ev-2").json()["evaluation"]
    assert again["attempt"] == 1
    refused(send(world, number, "passed", name="ev-1"), "conflict")


def test_a_lease_is_no_longer_than_the_suite_needs():
    world = registry_owned()
    evaluating(world)

    found = claim(world, lease=3600).json()["evaluation"]

    # Two cases of 600 seconds and a margin of 120: 22 minutes, not the hour asked for.
    assert found["claimExpiresAt"].startswith("2026-11-17T12:22")


def test_claim_is_for_the_evaluator_of_that_cluster_and_nothing_in_recovery():
    world = registry_owned()
    evaluating(world)

    assert claim(world, token="evaluator-elsewhere").status_code == 403
    assert claim(world, token="operator").status_code == 403
    assert claim(world, token=None).status_code == 401
    with world.store.transaction("test") as tx:
        tx.set_mode("recovery", "test")
        tx.event("recovery-entered")
    assert claim(world).json() == {"evaluation": None}


def test_a_result_from_another_evaluator_is_a_conflict():
    world = registry_owned()
    number = evaluating(world)
    claim(world, "ev-1")

    refused(send(world, number, "passed", name="ev-2"), "conflict")

    assert world.store.evaluations(DEP)[0].state == "claimed"
    assert world.store.revision(DEP, number).phase == "evaluating"
    assert send(world, number, "passed", attempt=9).status_code == 404


def test_a_repeated_result_is_not_recorded_twice():
    world = registry_owned()
    number = evaluating(world)
    claim(world, "ev-1")
    assert send(world, number, "passed").json() == {"recorded": True, "phase": "activated"}
    count = len(world.store.events(limit=10000))

    again = send(world, number, "passed")

    assert again.status_code == 200
    assert again.json() == {"recorded": False, "phase": "activated"}
    assert len(world.store.events(limit=10000)) == count
    refused(send(world, number, "failed"), "conflict")  # another result for a final evaluation


def test_the_revision_route_lists_its_evaluations_oldest_first():
    world = registry_owned()
    number = evaluating(world)
    evaluated(world, number, "error")

    found = world.get(f"/v1/deployments/{DEP}/revisions/{number}").json()["evaluations"]

    assert [(e["attempt"], e["state"]) for e in found] == [(1, "error"), (2, "pending")]


# -- rollback ------------------------------------------------------------------------------


def test_rollback_to_a_revoked_release_is_refused():
    world = registry_owned()
    first_release_live(world)
    revoke(world, release_name(world, 2))
    before = world.store.deployment(DEP)

    message = refused(world.post(ROLLBACK, {"revision": 2, "reason": "bad"}), "refused")

    assert message == f"release {release_name(world, 2)} is revoked (revocation-1)"
    assert world.store.deployment(DEP) == before


def test_rollback_to_a_release_whose_gates_no_longer_pass_is_refused():
    world = registry_owned()
    first_release_live(world)
    document = world.store.release(world.store.revision(DEP, 2).release_id).document
    broken = verdict(document, ready=True)
    broken["signatureVerified"] = False
    world.tick()
    world.cluster_shows(2, status=broken)

    message = refused(world.post(ROLLBACK, {"revision": 2, "reason": "bad"}), "refused")

    assert "signature" in message
    assert world.store.deployment(DEP).pinned_revision is None


def test_rollback_without_a_revision_picks_the_newest_earlier_one_that_is_not_revoked():
    world = registry_owned()
    first_release_live(world)
    revoke(world, release_name(world, 2))
    world.tick()
    world.cluster_shows(1)

    answer = world.post(ROLLBACK, {"revision": None, "reason": "it leaks"})

    assert answer.status_code == 200, answer.text
    deployment = world.store.deployment(DEP)
    assert (deployment.pinned_revision, deployment.current_revision) == (1, 1)
    event = last_event(world, "rolled-back")
    assert event.detail == {"from": 3, "to": 1, "reason": "it leaks"}
    assert deployment.generation == event.seq
    # Nothing before revision 1: refused. The pointer is on 1 already, so ask from there.
    refused(world.post(ROLLBACK, {"revision": None, "reason": "again"}), "refused")


# -- a new revision of one release ---------------------------------------------------------


def test_an_alias_that_resolves_to_a_new_model_release_makes_a_new_revision_of_the_same_release():
    world = registry_owned()
    number = first_release_live(world)
    assert revisions_re_resolve(world) is None  # nothing changed yet
    with world.store.transaction("feed") as tx:
        tx.put_item(Item(kind="model", name="qwen"))
        tx.put_deployment(
            Deployment(
                id="model:kubani:-:qwen",
                kind="model",
                item="qwen",
                cluster=CLUSTER,
                namespace="vllm",
                document={"servedName": "Qwen3.6-35B-A3B-NVFP4"},
            )
        )
        tx.put_alias(
            Alias(
                cluster=CLUSTER,
                alias="main",
                deployment_id="model:kubani:-:qwen",
                backend="llm-main",
            )
        )
        tx.event("feed-applied")

    created = revisions_re_resolve(world)

    assert created == number + 1
    old, new = world.store.revision(DEP, number), world.store.revision(DEP, created)
    assert new.release_id == old.release_id and new.origin == "registry"
    assert (new.phase, new.approval_id) == ("awaiting-runtime", None)
    assert [
        r.resolved_deployment for r in world.store.resolutions(new.id) if r.type == "model"
    ] == ["model:kubani:-:qwen"]
    event = last_event(world, "revision-recorded")
    assert event.detail["revision"] == created and event.detail["from"] == number
    assert revisions_re_resolve(world) is None  # and only once

    objects = [
        o
        for o in projection.objects_of(world.store, world.store.deployment(DEP))
        if o.kind == "AgentRelease"
    ]
    assert [o.name for o in objects] == [release_name(world, n) for n in (1, 2, number)]
    shared = objects[-1]
    assert shared.generation == new.generation
    assert shared.document["metadata"]["annotations"]["registry.agentplatform.io/revision"] == str(
        created
    )
    assert shared.document["spec"]["approval"]["approved"] is True  # the older revision's approval

    # It goes through the table like any release: carried forward, then activated.
    assert report(world, created) is True
    assert world.store.revision(DEP, created).approval_id == "approval-0001"
    assert report(world, created) is True
    assert phases(world)[-2:] == ["activated", "activated"]
    assert world.store.deployment(DEP).current_revision == created


def revisions_re_resolve(world: World) -> int | None:
    world.tick()
    with world.store.transaction("feed") as tx:
        return revisions.re_resolve(tx, DEP, now=world.now)


def test_a_revoked_release_is_projected_unapproved_with_the_revocation_named():
    world = registry_owned()
    first_release_live(world)
    revoke(world, release_name(world, 3))

    objects = projection.objects_of(world.store, world.store.deployment(DEP))

    revoked = next(o for o in objects if o.name == release_name(world, 3))
    assert revoked.document["spec"]["approval"] == {"approved": False}
    assert (
        revoked.document["metadata"]["annotations"]["registry.agentplatform.io/revoked"]
        == "revocation-1"
    )
    other = next(o for o in objects if o.name == release_name(world, 2))
    assert "registry.agentplatform.io/revoked" not in other.document["metadata"]["annotations"]


# -- status --------------------------------------------------------------------------------


def test_status_shows_current_candidate_acknowledged_pinned_and_revoked():
    world = registry_owned()
    assert world.post(PIN, {"revision": 2}).status_code == 200
    number = record(world, "a", lower_budget)
    report(world, number)
    revoke(world, release_name(world, 1))

    behind = status_of(world)
    assert (
        behind["acknowledged"] is False
    )  # the pin changed the Agent; the cluster has not taken it
    assert any("has not taken the last change" in b for b in behind["blockers"])
    world.tick()
    world.cluster_shows_agent()
    shown = status_of(world)

    assert shown["acknowledged"] is True
    assert shown["pinned"] == 2
    assert shown["current"] == {
        "release": release_name(world, 2),
        "revision": 2,
        "since": shown["current"]["since"],
        "admittedBy": None,
    }
    assert shown["candidate"] == {
        "release": release_name(world, number),
        "revision": number,
        "phase": "pending-approval",
        "reason": world.store.revision(DEP, number).reason,
        "evaluation": None,
    }
    assert [(r["id"], r["release"], r["reason"]) for r in shown["revoked"]] == [
        ("revocation-1", release_name(world, 1), "security")
    ]
    assert shown["lastRefused"] is None


def test_status_names_the_evaluation_of_a_candidate():
    world = registry_owned()
    evaluating(world)

    shown = status_of(world)

    assert shown["candidate"]["phase"] == "evaluating"
    assert shown["candidate"]["reason"] == "waiting for evaluation"
    assert shown["candidate"]["evaluation"] == {"attempt": 1, "state": "pending"}
    world.tick(601)
    assert any("evaluator is not running" in b for b in status_of(world)["blockers"])
    assert shown["current"]["revision"] == 2
