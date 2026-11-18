"""Milestone 2, from a release report to a current release, with no cluster.

A release is reported by its workflow, carried to the cluster, verified by the controller,
evaluated by the evaluator through the session interface, and made current, without a person
issuing a command. Around that: a failing candidate changes nothing, more authority waits for
a person, rollback and pin, revocation, a new model behind an alias, and an evaluator that dies.
"""

from __future__ import annotations

import copy
from typing import Any

from loopworld.sessions import COMPLETED
from loopworld.world import CLUSTER, PERSON, World
from test_milestone1 import AGENT, D, handed_over, running_platform

REQUEST = "Report the server version."
SUITE = {
    "schema_version": 1,
    "timeout_seconds": 600,
    "acceptance": {"min_passed": "all"},
    "cases": [{"id": "summary", "task": REQUEST, "expect": {"report_contains": ["PostgreSQL"]}}],
}
GOOD, BAD = "PostgreSQL 16.4 on x86_64, 3 databases", "no answer"


def passes(world: World, count: int = 1) -> None:
    for _ in range(count):
        world.settle()


def approve(world: World, number: int, approval_id: str) -> dict[str, Any]:
    """What a person does with `just agent approve`: preview, then approve what was previewed."""
    preview = world.read(f"/v1/deployments/{D}/revisions/{number}/approval", token="operator")
    assert preview["approvable"] is True, preview["gateFailures"]
    world.write(
        f"/v1/deployments/{D}/approvals",
        {
            "approvalId": approval_id,
            "revision": number,
            "authorityDigest": preview["authorityDigest"],
            "baseAuthorityDigest": preview["baseAuthorityDigest"],
            "policyRevision": preview["policyRevision"],
            "ticket": "LOOP-2",
        },
    )
    return preview


def current_release(world: World) -> World:
    """db-admin owned by the registry, as milestone 1 leaves it: a new release published by an
    operator, approved by a person and current. It carries its declaration, so that a later
    release that asks for more can be Ready beside it."""
    running_platform(world)
    handed_over(world)
    body = world.publish_declared(AGENT, "0.1.3", "a")
    body["lineage"] = {"repository": "X-McKay/zetra-agents", "tag": f"{AGENT}/v0.1.3"}
    assert world.write(f"/v1/deployments/{D}/releases", body)["revision"]["number"] == 3
    passes(world, 2)
    approve(world, 3, "loop-m2-base")
    passes(world, 3)
    seen = world.agent_status(AGENT)
    assert seen["current"]["revision"] == 3 and seen["candidate"] is None
    return world


def report(world: World, version: str, build: str, **how: Any) -> dict[str, Any]:
    """The release workflow reports a release. Returns the answer."""
    answer = world.report_release(world.publish_declared(AGENT, version, build, **how), version)
    assert answer.status_code == 200, answer.text
    return answer.json()


def evaluating(world: World, number: int) -> None:
    """Four passes: the cluster is given the release, the controller verifies it, the approval
    is carried forward, and the controller finds it Ready, which the registry then sees."""
    passes(world, 4)
    seen = world.agent_status(AGENT)["candidate"]
    assert (seen["revision"], seen["phase"]) == (number, "evaluating"), seen


def deliver(world: World, version: str, build: str, **how: Any) -> int:
    """A release that carries the suite is reported, verified and waiting for an evaluator."""
    world.temporal.end(REQUEST, COMPLETED, GOOD)
    number = report(world, version, build, suite=SUITE, **how)["revision"]
    evaluating(world, number)
    return number


def release_without_suite(world: World, version: str, build: str) -> None:
    """Reported with no suite: nothing to evaluate, so it is current once Ready, and the Agent
    names it on the pass after."""
    report(world, version, build)
    passes(world, 5)
    assert world.agent_status(AGENT)["acknowledged"] is True


def registry_events(world: World, after: int = 0) -> list[dict]:
    return world.read("/v1/events", deployment=D)["events"][after:]


def ready(world: World, name: str) -> str | None:
    found = (world.release(name) or {}).get("status", {}).get("conditions", [])
    return next((c["status"] for c in found if c["type"] == "Ready"), None)


def named_by_agent(world: World) -> str | None:
    annotations = world.agent(AGENT)["metadata"].get("annotations") or {}
    return annotations.get("registry.agentplatform.io/current-release")


def refused(answer, code: str, *words: str) -> None:
    assert answer.status_code in (403, 409), answer.text
    error = answer.json()["error"]
    assert error["code"] == code
    for word in words:
        assert word in error["message"], error["message"]


def test_a_release_is_delivered_with_no_command(world: World):
    current_release(world)
    old = world.agent_status(AGENT)["current"]["release"]
    mark = len(registry_events(world))
    world.temporal.end(REQUEST, COMPLETED, GOOD)

    # -- 1. the workflow reports the release: recorded, and nothing else moves ---------------
    body = world.publish_declared(AGENT, "0.1.4", "b", suite=SUITE)
    new = body["agentRelease"]["metadata"]["name"]
    answer = world.report_release(body, "0.1.4")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {"deployment": D, "release": new, "revision": 4, "created": True}
    assert world.report_release(body, "0.1.4").json()["created"] is False
    assert world.agent_status(AGENT)["current"]["release"] == old
    lineage = world.store.release(world.store.revision(D, 4).release_id).lineage
    assert (lineage["repository"], lineage["tag"]) == ("X-McKay/zetra-agents", f"{AGENT}/v0.1.4")
    assert lineage["evaluation"]["suite"] == SUITE

    # -- 2. projected with its declaration, verified by the controller ---------------------
    passes(world)
    projected = world.release(new)["spec"]
    assert (
        projected["declaration"]
        == world.cluster.defaulted(body["agentRelease"])["spec"]["declaration"]
    )
    passes(world)
    conditions = {c["type"]: c["status"] for c in world.release(new)["status"]["conditions"]}
    assert conditions["SignatureVerified"] == "True"

    # -- 3. its approval is carried forward; once Ready, an evaluation is requested --------
    passes(world, 2)
    evaluation = world.agent_status(AGENT)["candidate"]
    assert (evaluation["phase"], evaluation["evaluation"]) == (
        "evaluating",
        {"attempt": 1, "state": "pending"},
    )
    assert world.release(new)["spec"]["approval"]["approved"] is True
    assert world.agent_status(AGENT)["current"]["release"] == old

    # -- 4. the evaluator claims it and runs the suite through the session interface --------
    assert world.evaluate() is True
    assert [
        (s["release"], s["task"], s["purpose"], s["startedBy"]) for s in world.temporal.started()
    ] == [
        (
            new,
            {"request": REQUEST},
            "evaluation",
            f"service-account-registry-evaluator-{CLUSTER}",
        )
    ]
    assert world.evaluate() is False  # nothing else is waiting

    # -- 5. the verdict activated the revision; the cluster follows on the next pass -------
    seen = world.agent_status(AGENT)
    assert seen["current"]["release"] == new
    assert seen["current"]["admittedBy"] == f"{D}@4#1"
    assert named_by_agent(world) == old  # not before the provisioner writes it
    passes(world)
    assert named_by_agent(world) == new
    seen = world.agent_status(AGENT)
    assert (seen["acknowledged"], seen["blockers"], seen["nextActions"]) == (True, [], [])
    assert (seen["current"]["release"], seen["candidate"], seen["lastRefused"]) == (new, None, None)
    revision = world.read(f"/v1/deployments/{D}/revisions/4")
    assert [(e["attempt"], e["state"]) for e in revision["evaluations"]] == [(1, "passed")]

    # -- 6. and no person's command was part of it ------------------------------------------
    events = registry_events(world, mark)
    assert [e["type"] for e in events] == [
        "release-recorded",
        "runtime-verdict",
        "approval-carried-forward",
        "runtime-verdict",
        "evaluation-requested",
        "evaluation-claimed",
        "evaluation-recorded",
        "revision-activated",
    ]
    assert PERSON not in {e["actor"] for e in events}
    assert events[0]["actor"] == f"github:X-McKay/zetra-agents@{AGENT}/v0.1.4"
    assert events[-1]["detail"]["evaluation"]


def test_a_failing_candidate_changes_nothing_and_another_evaluation_can_be_asked_for(world: World):
    current_release(world)
    old = world.agent_status(AGENT)["current"]["release"]

    # -- 1. the suite fails: the revision is refused ----------------------------------------
    world.temporal.end(REQUEST, COMPLETED, BAD)
    number = report(world, "0.1.4", "b", suite=SUITE)["revision"]
    new = world.store.release(world.store.revision(D, number).release_id).name
    evaluating(world, number)
    assert world.evaluate() is True
    passes(world, 2)

    # -- 2. the pointer, the Agent and the old release are as they were ---------------------
    seen = world.agent_status(AGENT)
    assert seen["current"]["release"] == old
    assert named_by_agent(world) == old
    assert ready(world, old) == "True"
    assert world.store.deployment(D).current_revision == 3
    assert seen["candidate"] is None
    refusal = seen["lastRefused"]
    assert (refusal["release"], refusal["revision"]) == (new, number)
    assert refusal["evaluation"]["verdict"] == "failed"
    assert "0 of 1 cases passed" in refusal["evaluation"]["summary"]
    assert "evaluation failed" in refusal["reason"]
    assert seen["acknowledged"] is True
    assert world.store.revision(D, number).phase == "refused"
    refusal_event = registry_events(world)[-1]
    assert refusal_event["type"] == "revision-refused"

    # -- 3. an operator asks for another evaluation, and this one passes ---------------------
    world.temporal.end(REQUEST, COMPLETED, GOOD)
    world.write(f"/v1/deployments/{D}/revisions/{number}/evaluations", {"reason": "retry"})
    assert world.agent_status(AGENT)["candidate"]["evaluation"] == {
        "attempt": 2,
        "state": "pending",
    }
    assert world.evaluate() is True
    passes(world, 2)
    seen = world.agent_status(AGENT)
    assert seen["current"]["release"] == new
    assert seen["current"]["admittedBy"] == f"{D}@{number}#2"
    assert (seen["lastRefused"], seen["candidate"]) == (None, None)
    assert named_by_agent(world) == new
    revision = world.read(f"/v1/deployments/{D}/revisions/{number}")
    assert [(e["attempt"], e["state"]) for e in revision["evaluations"]] == [
        (1, "failed"),
        (2, "passed"),
    ]
    first, second = (s["sessionId"] for s in world.temporal.started())
    assert first != second  # the second attempt ran its own session


def test_a_release_that_asks_for_more_authority_waits_for_a_person(world: World):
    current_release(world)
    old = world.agent_status(AGENT)["current"]["release"]

    def one_more_tool(spec: dict) -> None:
        spec["capabilities"]["mcp"][0]["tools"].append("list_schemas")

    # -- 1. reported: it is not approved by anything the registry has seen -------------------
    world.temporal.end(REQUEST, COMPLETED, GOOD)
    number = report(world, "0.1.4", "b", suite=SUITE, change=one_more_tool)["revision"]
    new = world.store.release(world.store.revision(D, number).release_id).name
    passes(world, 3)
    seen = world.agent_status(AGENT)
    assert (seen["candidate"]["revision"], seen["candidate"]["phase"]) == (
        number,
        "pending-approval",
    )
    assert seen["candidate"]["reason"]
    assert any(new in b and "waits for approval" in b for b in seen["blockers"])
    assert seen["nextActions"] == [f"just agent approve {AGENT}"]
    assert world.evaluate() is False

    # -- 2. the current release keeps running: Ready, current, and the Agent as it was -------
    assert (seen["current"]["release"], named_by_agent(world)) == (old, old)
    assert ready(world, old) == "True"
    assert ready(world, new) == "False"
    assert world.agent(AGENT)["spec"]["capabilities"]["mcp"][0]["tools"] == ["execute_sql"]

    # -- 3. a person approves what was previewed -------------------------------------------
    preview = approve(world, number, "loop-m2-wider")
    assert preview["changes"]
    passes(world, 4)

    # -- 4. two declarations, two Ready releases; the new one is evaluated, then current ----
    assert (ready(world, old), ready(world, new)) == ("True", "True")
    assert world.release(old)["spec"]["declaration"] != world.release(new)["spec"]["declaration"]
    seen = world.agent_status(AGENT)
    assert (seen["current"]["release"], seen["candidate"]["phase"]) == (old, "evaluating")
    assert world.evaluate() is True
    passes(world, 2)
    seen = world.agent_status(AGENT)
    assert seen["current"]["release"] == new
    assert seen["current"]["admittedBy"] == f"{D}@{number}#1"
    assert named_by_agent(world) == new
    assert world.agent(AGENT)["spec"]["capabilities"]["mcp"][0]["tools"] == [
        "execute_sql",
        "list_schemas",
    ]
    assert (seen["blockers"], seen["nextActions"]) == ([], [])


def test_rollback_and_pin(world: World):
    current_release(world)
    first = world.agent_status(AGENT)["current"]["release"]

    # -- 1. a second release, with no suite, becomes current at once -----------------------
    release_without_suite(world, "0.1.4", "b")
    second = world.agent_status(AGENT)["current"]["release"]
    assert second != first
    assert world.agent_status(AGENT)["current"]["revision"] == 4

    # -- 2. rollback returns to the previous release, and holds it there -------------------
    world.write(f"/v1/deployments/{D}/rollback", {"revision": None, "reason": "it leaks"})
    passes(world, 2)
    seen = world.agent_status(AGENT)
    assert (seen["current"]["release"], seen["pinned"]) == (first, 3)
    assert named_by_agent(world) == first

    # -- 3. a revoked release is not a target -------------------------------------------------
    revocation = world.revoke(second, reason="quality")
    passes(world, 2)
    refusal = world.call(
        "POST", f"/v1/deployments/{D}/rollback", json={"revision": 4, "reason": "back"}
    )
    refused(refusal, "refused", second, revocation)
    assert world.agent_status(AGENT)["current"]["release"] == first

    # -- 4. while pinned, a newer revision that activates does not move the pointer ----------
    report(world, "0.1.5", "c")
    passes(world, 5)
    third = world.store.release(world.store.revision(D, 5).release_id).name
    assert world.store.revision(D, 5).phase == "activated"
    seen = world.agent_status(AGENT)
    assert (seen["current"]["release"], seen["pinned"]) == (first, 3)
    assert named_by_agent(world) == first
    assert ready(world, third) == "True"

    # -- 5. unpinning moves the pointer to the newest activated release that is not revoked --
    world.write(f"/v1/deployments/{D}/pin", {"revision": None})
    passes(world, 2)
    seen = world.agent_status(AGENT)
    assert (seen["current"]["release"], seen["pinned"]) == (third, None)
    assert named_by_agent(world) == third


def test_a_security_revocation_disables_the_release_before_it_cancels_its_sessions(world: World):
    current_release(world)
    release_without_suite(world, "0.1.4", "b")
    release_without_suite(world, "0.1.5", "c")
    older = world.store.release(world.store.revision(D, 4).release_id).name
    newest = world.store.release(world.store.revision(D, 5).release_id).name
    assert world.agent_status(AGENT)["current"]["release"] == newest

    # -- 1. sessions: finish. The release is disabled; its session is left to end ------------
    started = world.start_session(AGENT, "long task")
    assert started.status_code == 202, started.text
    assert world.start_session(AGENT, "long task", token="evaluator").status_code == 403
    world.revoke(newest, reason="security", sessions="finish")
    passes(world, 2)
    assert world.release(newest)["spec"]["approval"] == {"approved": False}
    assert world.temporal.cancelled == []
    assert len(world.temporal.running()) == 1
    assert world.agent_status(AGENT)["current"] is None

    # -- 2. sessions: cancel (the default for security). The write comes before the cancel ---
    world.write(f"/v1/deployments/{D}/rollback", {"revision": 4, "reason": "revoked"})
    passes(world, 2)
    assert world.agent_status(AGENT)["current"]["release"] == older
    assert world.start_session(AGENT, "second task").status_code == 202
    sessions = world.temporal.started()
    assert [s["release"] for s in sessions] == [newest, older]
    assert world.release(older)["spec"]["approval"]["approved"] is True
    world.revoke(older, reason="security")
    passes(world, 2)
    assert world.release(older)["spec"]["approval"] == {"approved": False}
    assert [(c.release, c.approval_then) for c in world.temporal.cancelled] == [
        (older, {"approved": False})
    ]
    assert world.temporal.cancelled[0].workflow_id == sessions[1]["workflowId"]
    # The session of the release that was revoked with `finish` is still running.
    assert world.temporal.running() == [sessions[0]["workflowId"]]
    before = len(world.temporal.cancelled)
    passes(world, 2)
    assert len(world.temporal.cancelled) == before  # nothing left to cancel


def test_a_new_model_release_behind_an_alias_is_evaluated_again(world: World):
    current_release(world)
    number = deliver(world, "0.1.4", "b")
    assert world.evaluate() is True
    passes(world, 2)
    release = world.store.revision(D, number).release_id
    assert world.agent_status(AGENT)["current"]["revision"] == number
    before = [s["sessionId"] for s in world.temporal.started()]
    assert len(before) == 1

    # -- 1. the feed points the alias at a new model release --------------------------------
    document = world.feed_document
    engine = copy.deepcopy(next(e for e in document["modelEngines"] if e["name"] == "main"))
    engine.update(
        name="main-next",
        servedName="Qwen3.7-NEXT",
        workload={**engine["workload"], "name": "vllm-next"},
    )
    engine["servedNameFrom"] = {**engine["servedNameFrom"], "key": "NEXT_MODEL_NAME"}
    document["modelEngines"].append(engine)
    for alias in document["aliases"]:
        if alias["alias"] == "main":
            alias["engine"] = "main-next"
    mark = len(registry_events(world))
    assert world.apply_feed() is True

    # -- 2. a new revision of the same release, which shares the one AgentRelease -----------
    newer = number + 1
    assert world.store.revision(D, newer).release_id == release
    assert world.store.revision(D, newer).origin == "registry"
    recorded = [e for e in registry_events(world, mark) if e["type"] == "revision-recorded"]
    assert [e["detail"]["revision"] for e in recorded] == [newer]
    passes(world, 3)
    name = world.store.release(release).name
    shared = [
        o
        for o in world.cluster.list("agentplatform.io/v1alpha1", "AgentRelease")
        if o["metadata"]["name"] == name
    ]
    assert len(shared) == 1
    annotations = shared[0]["metadata"]["annotations"]
    assert annotations["registry.agentplatform.io/revision"] == str(newer)
    assert shared[0]["spec"]["approval"]["approved"] is True

    # -- 3. it goes through evaluation again, and then it is current ------------------------
    seen = world.agent_status(AGENT)
    assert (seen["current"]["revision"], seen["candidate"]["phase"]) == (number, "evaluating")
    assert seen["candidate"]["revision"] == newer
    assert world.evaluate() is True
    passes(world, 2)
    seen = world.agent_status(AGENT)
    assert (seen["current"]["revision"], seen["current"]["release"]) == (newer, name)
    assert seen["current"]["admittedBy"] == f"{D}@{newer}#1"
    after = [s["sessionId"] for s in world.temporal.started()]
    assert len(after) == 2 and after[0] == before[0] and after[1] != after[0]
    assert {s["release"] for s in world.temporal.started()} == {name}


def test_an_evaluator_that_dies_leaves_its_evaluation_to_another(world: World):
    current_release(world)
    number = deliver(world, "0.1.4", "b")

    def claim(who: str) -> dict | None:
        answer = world.call(
            "POST",
            f"/v1/clusters/{CLUSTER}/evaluations/claim",
            token="evaluator",
            json={"evaluator": who, "leaseSeconds": 3600},
        )
        assert answer.status_code == 200, answer.text
        return answer.json()["evaluation"]

    # -- 1. claimed and never finished: held until the lease runs out -------------------------
    held = claim("dies")
    assert (held["revision"], held["attempt"]) == (number, 1)
    lease = 600 * len(SUITE["cases"]) + 120  # the registry gives the smaller of 3600 and this
    world.tick(lease - 1)
    assert claim("other") is None
    world.tick(2)
    again = claim("other")
    assert (again["revision"], again["attempt"]) == (number, 1)
    assert again["claimExpiresAt"] > held["claimExpiresAt"]
    world.tick(lease + 1)  # and that one is not finished either

    # -- 2. a second evaluator cycle runs while the first is still waiting on its session ----
    sessions_seen: list[int] = []

    def meanwhile(seconds: float) -> None:
        """The first evaluator sleeps; the lease runs out and another evaluator cycle runs."""
        if sessions_seen:
            world.tick(seconds)
            return
        sessions_seen.append(len(world.temporal.started()))
        world.tick(lease + 1)
        (running,) = world.temporal.running()
        world.temporal.finish(running, GOOD)
        assert world.evaluate("second") is True

    world.temporal.endings.clear()  # the session of the first cycle does not finish by itself
    first = world.worker("first", sleep=meanwhile)
    assert first.run_pass() is True

    # One session was started for the attempt, and the second cycle read the same one.
    assert sessions_seen == [1]
    started = world.temporal.started()
    assert len(started) == 1
    assert (
        started[0]["release"]
        == world.store.release(world.store.revision(D, number).release_id).name
    )
    assert world.agent_status(AGENT)["current"]["revision"] == number
    revision = world.read(f"/v1/deployments/{D}/revisions/{number}")
    (recorded,) = revision["evaluations"]
    assert (recorded["state"], recorded["claimedBy"]) == ("passed", "second")
    assert first.unposted is None  # the first cycle's late verdict was refused and dropped
