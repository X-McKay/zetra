"""Dependencies section 7.3: cancel the task sessions of a held agent after its Agent is
written, and report how many task sessions are still running."""

import copy
import logging

import httpx

from registry_provisioner.main import main
from registry_provisioner.registry import HttpRegistry, RegistryError
from registry_provisioner.sessions import HttpSessions
from tests.fakes import FakeSessions

NS = "tenant-test"
DEPLOYMENT = "agent:kubani:tenant-test:db-admin"
REASON = "alias move mv-1"


def hold(mode="cancel", move="mv-1", agent="db-admin", deployment=DEPLOYMENT):
    return {
        "move": move,
        "deployment": deployment,
        "tenant": NS,
        "namespace": NS,
        "agent": agent,
        "sessions": mode,
    }


def with_holds(desired, *holds):
    desired = copy.deepcopy(desired)
    desired["holds"] = list(holds)
    return desired


def run(kube, registry, env, sessions):
    return main(["--once"], kube=kube, registry=registry, sessions=sessions, environ=env)


def running(*purposes):
    return [{"sessionId": f"s{i}", "purpose": p} for i, p in enumerate(purposes)]


def test_cancel_hold_cancels_task_sessions_after_the_agent_write_and_reports_the_rest(
    kube, registry, desired, env
):
    registry.answer = with_holds(desired, hold("cancel"))
    sessions = FakeSessions(kube)
    sessions.running = running("task", "task", "evaluation")
    run(kube, registry, env, sessions)
    order = [
        e
        for e in kube.log
        if e[0] in ("cancel-sessions", "list") or e[:3] == ("write", "Agent", "db-admin")
    ]
    assert [e[0] for e in order] == ["write", "cancel-sessions", "list"]
    assert sessions.purpose_calls == [(NS, "db-admin", "task", REASON)]
    assert registry.reports[-1]["holds"] == [
        {"move": "mv-1", "deployment": DEPLOYMENT, "running": 0}
    ]


def test_finish_hold_cancels_nothing_and_counts_task_sessions_only(kube, registry, desired, env):
    registry.answer = with_holds(desired, hold("finish"))
    sessions = FakeSessions(kube)
    sessions.running = running("task", "evaluation", "task", "evaluation")
    run(kube, registry, env, sessions)
    assert sessions.purpose_calls == [] and sessions.calls == []
    assert registry.reports[-1]["holds"] == [
        {"move": "mv-1", "deployment": DEPLOYMENT, "running": 2}
    ]


def test_hold_is_left_out_when_the_agent_write_fails_and_the_others_are_reported(
    kube, registry, desired, env
):
    other = copy.deepcopy(desired["deployments"][0])
    other["deployment"] = "agent:kubani:tenant-test:other"
    other["objects"] = [copy.deepcopy(o) for o in other["objects"][:1]]
    other["objects"][0]["name"] = other["objects"][0]["document"]["metadata"]["name"] = "other"
    desired = copy.deepcopy(desired)
    desired["deployments"].append(other)
    registry.answer = with_holds(
        desired,
        hold("cancel"),
        hold("cancel", "mv-2", "other", other["deployment"]),
    )
    kube.fail_apply = {"db-admin"}
    sessions = FakeSessions(kube)
    run(kube, registry, env, sessions)
    assert sessions.purpose_calls == [(NS, "other", "task", "alias move mv-2")]
    assert registry.reports[-1]["holds"] == [
        {"move": "mv-2", "deployment": other["deployment"], "running": 0}
    ]


def test_hold_whose_sessions_cannot_be_listed_or_cancelled_is_left_out_and_retried(
    kube, registry, desired, env, caplog
):
    registry.answer = with_holds(desired, hold("cancel"))
    sessions = FakeSessions(kube)
    sessions.list_error = RegistryError("list sessions answered 503")
    caplog.set_level(logging.INFO, logger="registry_provisioner")
    assert run(kube, registry, env, sessions) == 0
    assert registry.reports[-1]["holds"] == []
    assert "mv-1" in caplog.text and "will be tried again" in caplog.text
    sessions.list_error = None
    sessions.error = RegistryError("cancel by release answered 503")
    run(kube, registry, env, sessions)
    assert registry.reports[-1]["holds"] == []
    sessions.error = None
    run(kube, registry, env, sessions)
    assert len(registry.reports[-1]["holds"]) == 1


def test_no_holds_key_in_the_answer_means_none_in_the_report(kube, registry, desired, env):
    sessions = FakeSessions(kube)
    run(kube, registry, env, sessions)
    assert "holds" not in registry.reports[-1]
    assert sessions.purpose_calls == []


def test_no_session_interface_means_no_hold_is_reported(kube, registry, desired, env, caplog):
    registry.answer = with_holds(desired, hold("cancel"))
    with caplog.at_level(logging.INFO, logger="registry_provisioner"):
        run(kube, registry, env, None)
    assert registry.reports[-1]["holds"] == []
    assert "no session interface configured" in caplog.text


def test_http_sessions_send_the_list_and_the_cancel_the_contract_describes():
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"sessions": running("task")})
        return httpx.Response(200, json={"cancelled": ["s1"], "failed": []})

    class Tokens:
        def token(self):
            return "t"

    http = httpx.Client(transport=httpx.MockTransport(handler))
    sessions = HttpSessions(
        "http://session-api-{tenant}.agent-platform.svc:8080",
        HttpRegistry("https://registry.test", "kubani", Tokens(), http),
    )
    assert sessions.list_running(NS, "db-admin") == running("task")
    assert sessions.cancel_sessions(NS, "db-admin", "task", REASON) == 1
    listing, cancel = seen
    base = "http://session-api-tenant-test.agent-platform.svc:8080/v1/tenants/tenant-test/sessions"
    assert str(listing.url) == base + "?state=running&agent=db-admin"
    assert listing.headers["authorization"] == "Bearer t"
    assert str(cancel.url) == base + "/cancel"
    assert cancel.read() == b'{"agent":"db-admin","purpose":"task","reason":"%s"}' % REASON.encode()
