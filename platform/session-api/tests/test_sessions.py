"""The ten items of `contract/sessions.md` section 6 (item 10 is in the runner's tests)."""

from __future__ import annotations

import json

import pytest
from runner.types import SessionInput, SessionResult

from tests import fakes
from tests.conftest import TENANT

SID = "6f1d2c3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
SID2 = "7a1d2c3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
SID3 = "8b1d2c3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"


def code(answer):
    return answer.json()["error"]["code"]


def session_input(release="db-admin-r1", sid=SID, agent="db-admin"):
    return SessionInput(TENANT, agent, release, sid, None, {"prompt": "go"})


# 1


def test_a_session_is_started_and_read_back_with_its_memo(world):
    answer = world.start(sessionId=SID)
    assert answer.status_code == 202
    assert answer.json() == {
        "sessionId": SID,
        "agent": "db-admin",
        "release": "db-admin-r1",
        "workflowId": f"session:{TENANT}:db-admin:{SID}",
        "state": "running",
        "created": True,
    }
    inp, memo = world.sessions.submitted[0]
    assert (inp.grant_handle, inp.worker_pool) == (None, "legacy")
    assert memo == {"release": "db-admin-r1", "startedBy": "al@example.com", "purpose": "task"}
    read = world.call("GET", f"/sessions/db-admin/{SID}")
    assert read.status_code == 200
    assert read.json() | {"progress": None} == {
        "sessionId": SID,
        "agent": "db-admin",
        "release": "db-admin-r1",
        "purpose": "task",
        "startedBy": "al@example.com",
        "state": "running",
        "outcome": None,
        "startedAt": "2026-11-20T09:00:00Z",
        "finishedAt": None,
        "progress": None,
        "report": None,
        "reportProblem": None,
    }
    assert read.json()["progress"] == world.sessions.progress


# 2


def test_no_token_is_not_signed_in(world):
    answer = world.start(who=None)
    assert (answer.status_code, code(answer)) == (401, "not-signed-in")
    assert world.call("GET", f"/sessions/db-admin/{SID}", None).status_code == 401
    assert world.call("GET", "/sessions?state=running", "unknown").status_code == 401


@pytest.mark.parametrize(
    "who, method, path, body",
    [
        ("reader", "POST", "/sessions", {"agent": "db-admin", "task": 1}),
        ("reader", "GET", f"/sessions/db-admin/{SID}", None),
        ("reader", "GET", "/sessions?state=running", None),
        ("other-evaluator", "POST", "/sessions", {"agent": "db-admin", "task": 1}),
        ("other-evaluator", "GET", f"/sessions/db-admin/{SID}", None),
        ("other-provisioner", "GET", "/sessions?state=running", None),
        (
            "other-provisioner",
            "POST",
            "/sessions/cancel",
            {"agent": "a", "release": "r", "reason": "x"},
        ),
        ("provisioner", "POST", "/sessions", {"agent": "db-admin", "task": 1}),
        ("evaluator", "GET", "/sessions?state=running", None),
    ],
)
def test_a_token_that_may_not_do_this_is_refused(world, who, method, path, body):
    answer = world.call(method, path, who, body)
    assert (answer.status_code, code(answer)) == (403, "refused")
    assert world.sessions.submitted == [] and world.sessions.cancelled == []


def test_another_tenant_is_not_found_before_the_token_is_looked_at(world):
    answer = world.http.get("/v1/tenants/tenant-other/sessions?state=running")
    assert (answer.status_code, code(answer)) == (404, "not-found")


# 3


def test_only_the_evaluator_may_name_a_release_that_is_not_current(world):
    world.objects.releases["db-admin-r0"] = fakes.release("db-admin-r0")
    refused = world.start(release="db-admin-r0")
    assert (refused.status_code, code(refused)) == (403, "refused")
    assert "only the evaluator" in refused.json()["error"]["message"]
    assert world.sessions.submitted == []
    allowed = world.start("evaluator", release="db-admin-r0", purpose="evaluation")
    assert allowed.status_code == 202 and allowed.json()["release"] == "db-admin-r0"
    assert world.start(release="db-admin-r1").status_code == 202  # naming the current one is fine


# 4


def mutate(w, which):
    if which == "no agent":
        del w.objects.agents["db-admin"]
    elif which == "not owned":
        w.objects.agents["db-admin"] = fakes.agent(owned=False)
    elif which == "disabled":
        w.objects.agents["db-admin"] = fakes.agent(enabled=False)
    elif which == "no current release":
        w.objects.agents["db-admin"] = fakes.agent(current=None)
    elif which == "no release":
        del w.objects.releases["db-admin-r1"]
    elif which == "release of another agent":
        w.objects.releases["db-admin-r1"] = fakes.release(of="other")
    elif which == "not ready":
        w.objects.releases["db-admin-r1"] = fakes.release(ready=False)
    elif which == "acts for a person":
        w.objects.agents["db-admin"] = fakes.agent(mode="on-behalf-of")
    elif which == "declaration acts for a person":
        w.objects.releases["db-admin-r1"] = fakes.release(
            declaration={"identity": {"mode": "on-behalf-of"}}
        )


@pytest.mark.parametrize(
    "which, status, error, text",
    [
        ("no agent", 404, "not-found", "no Agent db-admin"),  # 3
        (
            "not owned",
            409,
            "wrong-path",
            "db-admin is not owned by the registry; use the pull",
        ),  # 4
        ("disabled", 409, "refused", "the agent is disabled"),  # 5
        ("no current release", 409, "refused", "the agent has no current release"),  # 6
        ("no release", 404, "not-found", "no AgentRelease db-admin-r1"),  # 8
        ("release of another agent", 404, "not-found", "no AgentRelease db-admin-r1"),  # 8
        (
            "not ready",
            409,
            "refused",
            "release db-admin-r1 is not Ready: SignatureMissing: no sig",
        ),  # 9
        ("acts for a person", 409, "refused", "needs that person's consent"),  # 10
        ("declaration acts for a person", 409, "refused", "needs that person's consent"),  # 10
    ],
)
def test_each_check_refuses_and_submits_nothing(world, which, status, error, text):
    mutate(world, which)
    answer = world.start()
    assert (answer.status_code, code(answer)) == (status, error)
    assert text in answer.json()["error"]["message"]
    assert world.sessions.submitted == []


def test_the_declaration_decides_the_identity_mode_when_there_is_one(world):
    world.objects.agents["db-admin"] = fakes.agent(mode="on-behalf-of")
    world.objects.releases["db-admin-r1"] = fakes.release(
        declaration={"identity": {"mode": "agent"}}
    )
    assert world.start().status_code == 202


# 5


def test_the_same_session_id_starts_one_session_and_another_task_conflicts(world):
    first = world.start(sessionId=SID)
    again = world.start(sessionId=SID)
    assert (first.status_code, again.status_code) == (202, 200)
    assert again.json()["created"] is False and again.json()["state"] == "running"
    assert len(world.sessions.submitted) == 1
    other = world.start(sessionId=SID, task={"prompt": "something else"})
    assert (other.status_code, code(other)) == (409, "conflict")
    assert len(world.sessions.submitted) == 1


# 6


def finished(world, status, outcome=None, snapshot=None):
    result = SessionResult(outcome, snapshot=snapshot) if outcome else None
    world.sessions.add(session_input(), {"release": "db-admin-r1"}, status, result)


@pytest.mark.parametrize(
    "status, outcome, state, shown",
    [
        ("RUNNING", None, "running", None),
        ("COMPLETED", "COMPLETED", "finished", "completed"),
        ("COMPLETED", "FAILED", "finished", "failed"),
        ("COMPLETED", "TIMED_OUT", "finished", "timed-out"),
        ("COMPLETED", "NOT_CONFIRMED", "finished", "not-confirmed"),
        ("CANCELED", None, "finished", "cancelled"),
        ("FAILED", None, "finished", "failed"),
        ("TERMINATED", None, "finished", "failed"),
        ("TIMED_OUT", None, "finished", "failed"),
    ],
)
def test_each_row_of_the_state_table(world, status, outcome, state, shown):
    finished(world, status, outcome)
    answer = world.call("GET", f"/sessions/db-admin/{SID}").json()
    assert (answer["state"], answer["outcome"]) == (state, shown)


def test_progress_is_null_when_the_query_fails_or_the_session_is_over(world):
    finished(world, "RUNNING")
    world.sessions.progress = RuntimeError("no worker")
    assert world.call("GET", f"/sessions/db-admin/{SID}").json()["progress"] is None
    finished(world, "COMPLETED", "COMPLETED")
    world.sessions.progress = {"state": "x"}
    assert world.call("GET", f"/sessions/db-admin/{SID}").json()["progress"] is None


def test_the_report_is_read_and_a_missing_one_is_not_an_error(world):
    key = "a" * 64
    finished(world, "COMPLETED", "COMPLETED", snapshot=key)
    world.reports.reports[key] = "# done"
    answer = world.call("GET", f"/sessions/db-admin/{SID}").json()
    assert (answer["report"], answer["reportProblem"]) == ("# done", None)
    del world.reports.reports[key]
    answer = world.call("GET", f"/sessions/db-admin/{SID}")
    assert answer.status_code == 200
    assert answer.json()["report"] is None and "report missing" in answer.json()["reportProblem"]
    finished(world, "CANCELED")
    assert world.call("GET", f"/sessions/db-admin/{SID}").json()["reportProblem"]


def test_a_session_with_no_memo_reads_its_release_from_its_input(world):
    world.sessions.add(session_input("db-admin-r0"), None)
    answer = world.call("GET", f"/sessions/db-admin/{SID}").json()
    assert (answer["release"], answer["purpose"], answer["startedBy"]) == (
        "db-admin-r0",
        "task",
        None,
    )
    assert world.call("GET", f"/sessions/db-admin/{SID2}").status_code == 404


# 7


def test_cancel_by_release_cancels_a_session_with_no_memo_and_leaves_other_releases(world):
    world.sessions.add(session_input("db-admin-r1", SID), None)  # no memo; input names it
    world.sessions.add(session_input("db-admin-r1", SID2), {"release": "db-admin-r1"})
    world.sessions.add(session_input("db-admin-r0", SID3), {"release": "db-admin-r0"})
    listed = world.call("GET", "/sessions?state=running&release=db-admin-r1", "provisioner")
    assert {s["sessionId"] for s in listed.json()["sessions"]} == {SID, SID2}
    answer = world.call(
        "POST",
        "/sessions/cancel",
        "provisioner",
        {"agent": "db-admin", "release": "db-admin-r1", "reason": "release withdrawn"},
    )
    assert answer.status_code == 200
    assert sorted(answer.json()["cancelled"]) == sorted([SID, SID2])
    assert answer.json()["failed"] == []
    assert world.sessions.cancelled == [f"session:{TENANT}:db-admin:{s}" for s in (SID, SID2)]


def test_cancel_by_purpose_and_without_a_release(world):
    world.sessions.add(session_input("db-admin-r1", SID), {"release": "db-admin-r1"})
    world.sessions.add(
        session_input("db-admin-r1", SID2),
        {"release": "db-admin-r1", "purpose": "evaluation"},
    )
    world.sessions.add(session_input("db-admin-r0", SID3), {"release": "db-admin-r0"})

    def cancel(who, body):
        return world.call(
            "POST", "/sessions/cancel", who, {"agent": "db-admin", "reason": "x"} | body
        )

    one = cancel("provisioner", {"release": "db-admin-r1", "purpose": "task"})
    assert one.json()["cancelled"] == [SID]
    every = cancel("provisioner", {"purpose": "task"})  # across releases; SID is cancelled again
    assert sorted(every.json()["cancelled"]) == sorted([SID, SID3])
    assert f"session:{TENANT}:db-admin:{SID2}" not in world.sessions.cancelled  # not a task
    everything = cancel("operator", {})
    assert sorted(everything.json()["cancelled"]) == sorted([SID, SID2, SID3])
    bad = cancel("provisioner", {"purpose": "other"})
    assert (bad.status_code, code(bad)) == (400, "invalid-arguments")


# 8


def test_a_held_agent_refuses_an_operators_start_and_not_the_evaluators(world):
    world.objects.agents["db-admin"] = fakes.agent(held="mv-1")
    refused = world.start()
    assert (refused.status_code, code(refused)) == (409, "refused")
    assert refused.json()["error"]["message"] == "the agent is held by alias move mv-1"
    assert world.sessions.submitted == []
    allowed = world.start("evaluator", purpose="evaluation")
    assert allowed.status_code == 202
    assert len(world.sessions.submitted) == 1


def test_the_evaluator_cannot_read_or_cancel_a_task_session(world):
    assert world.start(sessionId=SID).status_code == 202
    assert world.start("evaluator", sessionId=SID2, purpose="evaluation").status_code == 202
    for path, method, body in [
        (f"/sessions/db-admin/{SID}", "GET", None),
        (f"/sessions/db-admin/{SID}/cancel", "POST", {"reason": "no"}),
    ]:
        answer = world.call(method, path, "evaluator", body)
        assert (answer.status_code, code(answer)) == (403, "refused")
    assert world.sessions.cancelled == []
    assert world.call("GET", f"/sessions/db-admin/{SID2}", "evaluator").status_code == 200
    answer = world.call("POST", f"/sessions/db-admin/{SID2}/cancel", "evaluator", {"reason": "x"})
    assert answer.json() == {"sessionId": SID2, "cancelRequested": True}
    assert world.sessions.cancelled == [f"session:{TENANT}:db-admin:{SID2}"]


def test_cancel_of_a_finished_session_asks_for_nothing(world):
    finished(world, "COMPLETED", "COMPLETED")
    answer = world.call("POST", f"/sessions/db-admin/{SID}/cancel", json={"reason": "late"})
    assert answer.json() == {"sessionId": SID, "cancelRequested": False}
    assert world.sessions.cancelled == []


# 9


def test_the_record_of_a_start_has_no_task_text(world, capsys):
    world.start(sessionId=SID, task={"prompt": "SECRET-TASK-TEXT"})
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and "SECRET-TASK-TEXT" not in lines[0]
    assert "Bearer" not in lines[0]
    assert json.loads(lines[0]) == {
        "time": "2026-11-20T09:00:00Z",
        "route": "POST /v1/tenants/{tenant}/sessions",
        "caller": "al@example.com",
        "role": "operator",
        "agent": "db-admin",
        "release": "db-admin-r1",
        "sessionId": SID,
        "purpose": "task",
        "code": "ok",
    }
    world.start(sessionId=SID2, who="reader")
    assert json.loads(capsys.readouterr().out)["code"] == "refused"


# Unreachable seams


def test_an_unreachable_seam_is_unavailable_and_retryable(world):
    world.sessions.down = True
    answer = world.start()
    assert (answer.status_code, code(answer)) == (503, "unavailable")
    assert answer.json()["error"]["retryable"] is True
