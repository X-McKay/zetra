import re
import threading

import pytest

from registry_evaluator.cycle import Worker
from registry_evaluator.evaluate import Runtime, report_text, session_id
from registry_evaluator.main import main, run_loop
from registry_evaluator.registry import RegistryError
from registry_evaluator.sessions import SessionError
from tests.fakes import Clock, FakeRegistry, FakeSessions, case, evaluation


def worker(registry, sessions, clock=None, name="ev-1"):
    clock = clock or Clock()
    return Worker(name, registry, Runtime(sessions, clock, clock.sleep)), clock


def test_passing_suite_is_recorded_once_as_passed():
    registry = FakeRegistry(evaluation([case("a", report_contains=["ok"]), case("b")]))
    sessions = FakeSessions()
    w, _ = worker(registry, sessions)
    assert w.run_pass() is True
    assert w.run_pass() is False  # nothing more to claim
    (posted,) = registry.results
    assert posted["verdict"] == "passed"
    assert posted["summary"] == "2 of 2 cases passed"
    assert [c["id"] for c in posted["cases"]] == ["a", "b"]
    assert posted["evaluator"] == "ev-1" and posted["attempt"] == 1
    assert registry.claims[0] == ("ev-1", 3600)
    assert all(re.fullmatch(r"[0-9a-f-]{36}", s["sessionId"]) for s in sessions.starts)


def test_failing_case_is_recorded_once_as_failed_naming_the_check():
    registry = FakeRegistry(
        evaluation([case("good"), case("blocking", report_contains=["blocked by"])])
    )
    w, _ = worker(registry, FakeSessions())
    w.run_pass()
    (posted,) = registry.results
    assert posted["verdict"] == "failed"
    assert posted["summary"] == "1 of 2 cases passed; acceptance needs all"
    bad = posted["cases"][1]
    assert bad["passed"] is False and bad["outcome"] == "completed"
    assert bad["detail"] == 'report_contains: "blocked by" not found'


def test_case_past_its_time_is_cancelled_and_recorded_once_as_error():
    registry = FakeRegistry(evaluation([case("slow"), case("never-run")], timeout=30))
    sessions = FakeSessions({"do slow": ("completed", "ok", None)})
    w, clock = worker(registry, sessions)
    w.run_pass()
    (posted,) = registry.results
    assert posted["verdict"] == "error"
    assert "slow" in posted["summary"] and "cancelled" in posted["summary"]
    assert len(sessions.cancelled) == 1
    assert [c["id"] for c in posted["cases"]] == ["slow"]
    assert 30 <= sum(clock.slept) < 40


def test_start_refused_is_recorded_as_error_with_the_code():
    registry = FakeRegistry(evaluation([case("a")]))
    sessions = FakeSessions()
    sessions.refuse = SessionError(
        "release R is not Ready: Failed: nope", status=409, code="refused"
    )
    w, clock = worker(registry, sessions)
    w.run_pass()
    (posted,) = registry.results
    assert posted["verdict"] == "error"
    assert "refused" in posted["summary"] and "not Ready" in posted["summary"]
    assert len(sessions.starts) == 1 and clock.slept == []  # a 4xx is not retried


def test_unreachable_interface_is_tried_five_times_then_error():
    registry = FakeRegistry(evaluation([case("a")]))
    sessions = FakeSessions()
    sessions.down = 99
    w, clock = worker(registry, sessions)
    w.run_pass()
    assert registry.results[0]["verdict"] == "error"
    assert sum(clock.slept) == 60 and sessions.down == 99 - 5


def test_second_evaluator_on_the_same_evaluation_starts_no_second_session():
    ev = evaluation([case("a"), case("b")])
    sessions = FakeSessions()
    first = FakeRegistry(ev)
    second = FakeRegistry(ev)  # the registry hands the evaluation out again after a lease
    worker(first, sessions, name="ev-1")[0].run_pass()
    worker(second, sessions, name="ev-2")[0].run_pass()
    assert len(sessions.sessions) == 2
    assert [s["sessionId"] for s in sessions.starts[:2]] == [
        s["sessionId"] for s in sessions.starts[2:]
    ]
    assert first.results[0]["cases"] == second.results[0]["cases"]
    assert sessions.starts[0]["sessionId"] == session_id(ev, "a")


def test_nothing_to_claim_starts_nothing_and_the_loop_sleeps():
    registry = FakeRegistry()
    sessions = FakeSessions()
    w, _ = worker(registry, sessions)
    stop = threading.Event()
    waits = []
    stop.wait = lambda t=None: waits.append(t) or stop.set()
    run_loop(w.run_pass, 15.0, stop)
    assert sessions.starts == [] and registry.results == [] and waits == [15.0]


def test_min_passed_one_of_two_with_one_passing_is_passed():
    registry = FakeRegistry(
        evaluation([case("a"), case("b", report_contains=["absent"])], min_passed=1)
    )
    w, _ = worker(registry, FakeSessions())
    w.run_pass()
    posted = registry.results[0]
    assert posted["verdict"] == "passed" and posted["summary"] == "1 of 2 cases passed"


def test_result_not_accepted_is_posted_again_without_running_the_cases_again():
    registry = FakeRegistry(evaluation([case("a")]))
    registry.result_errors = [RegistryError("down", status=503), RegistryError("gone")]
    sessions = FakeSessions()
    w, _ = worker(registry, sessions)
    assert w.run_pass() is True  # ran; the post failed
    assert w.run_pass() is False  # the network error again
    assert w.run_pass() is True  # accepted
    assert len(registry.results) == 1 and len(sessions.starts) == 1
    assert len(registry.claims) == 1


def test_recorded_false_is_accepted():
    registry = FakeRegistry(evaluation([case("a")]))
    registry.answer = {"recorded": False, "phase": "superseded"}
    w, _ = worker(registry, FakeSessions())
    w.run_pass()
    assert w.unposted is None and len(registry.results) == 1


def test_checks_use_the_string_or_compact_json_and_each_of_the_three_checks():
    assert report_text({"a": [1, 2]}) == '{"a":[1,2]}'
    registry = FakeRegistry(
        evaluation(
            [
                case("m", report_matches=[r"databases?:\s+\d+"], report_excludes=["Traceback"]),
                case("x", report_excludes=["Traceback"]),
            ]
        )
    )
    sessions = FakeSessions(
        {"do m": ("completed", "databases: 4", 1), "do x": ("completed", "Traceback ...", 0)}
    )
    w, _ = worker(registry, sessions)
    w.run_pass()
    cases = registry.results[0]["cases"]
    assert cases[0]["passed"] and not cases[1]["passed"]
    assert cases[1]["detail"] == 'report_excludes: "Traceback" found'


def test_main_rejects_bad_settings(capsys):
    assert main([], environ={}) == 2
    env = {
        "EVALUATOR_CLUSTER": "kubani",
        "EVALUATOR_REGISTRY_URL": "https://registry.test",
        "EVALUATOR_SESSION_API_URL": "http://session-api.agent-platform.svc:8080",
    }
    assert main([], environ=env) == 2
    assert "{tenant}" in capsys.readouterr().err


@pytest.mark.parametrize("once,code", [(True, 1)])
def test_main_once_with_nothing_to_claim(once, code):
    env = {
        "EVALUATOR_CLUSTER": "kubani",
        "EVALUATOR_REGISTRY_URL": "https://registry.test",
        "EVALUATOR_SESSION_API_URL": "http://session-api-{tenant}.agent-platform.svc:8080",
    }
    registry = FakeRegistry()
    assert main(["--once"], registry=registry, sessions=FakeSessions(), environ=env) == code


def test_http_clients_call_the_documented_paths_and_read_error_bodies(tmp_path):
    import httpx

    from registry_evaluator.registry import HttpRegistry, TokenSource
    from registry_evaluator.sessions import HttpSessions

    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 300})
        seen.append((request.method, str(request.url), request.headers["authorization"]))
        if request.url.path.endswith("/sessions"):
            err = {"error": {"code": "refused", "message": "not Ready", "retryable": False}}
            return httpx.Response(409, json=err)
        return httpx.Response(200, json={"evaluation": None, "recorded": True})

    http = httpx.Client(transport=httpx.MockTransport(handle))
    token_file = tmp_path / "token"
    token_file.write_text("sa")
    tokens = TokenSource(
        http, "https://idp.test/token", "registry-evaluator-kubani", str(token_file)
    )
    registry = HttpRegistry("https://registry.test", "kubani", tokens, http)
    assert registry.claim("ev-1", 3600) is None
    registry.result("agent:kubani:t:a", 3, 1, {"verdict": "passed"})
    sessions = HttpSessions("http://session-api-{tenant}.agent-platform.svc:8080", tokens, http)
    with pytest.raises(SessionError) as err:
        sessions.start("tenant-test", {})
    assert err.value.code == "refused" and err.value.status == 409
    urls = [u for _, u, _ in seen]
    assert urls == [
        "https://registry.test/v1/clusters/kubani/evaluations/claim",
        "https://registry.test/v1/deployments/agent%3Akubani%3At%3Aa/revisions/3/evaluations/1/result",
        "http://session-api-tenant-test.agent-platform.svc:8080/v1/tenants/tenant-test/sessions",
    ]
    assert {a for _, _, a in seen} == {"Bearer t"}
