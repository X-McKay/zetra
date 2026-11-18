"""The release action's last step, `report.py`, run with fakes for HTTP and the clock."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest
import yaml

ACTION = Path(__file__).parents[3] / ".github" / "actions" / "agent-release"
spec = importlib.util.spec_from_file_location("agent_release_report", ACTION / "report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

SECRET = "eyJ.secret-identity-token.sig"
SUITE = {
    "schema_version": 1,
    "timeout_seconds": 60,
    "acceptance": {"min_passed": "all"},
    "cases": [{"id": "one", "task": "Say hello."}],
}
GOOD = {"deployment": "agent:kubani:tenant-test:db-admin", "release": "r-1", "revision": 3}


class Fake:
    """GitHub answers the token request; the registry answers from `answers`, in order."""

    def __init__(self, *answers: tuple[int, dict] | OSError):
        self.answers = list(answers)
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.slept: list[float] = []

    def http(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        if method == "GET":
            return 200, json.dumps({"value": SECRET}).encode()
        answer = self.answers.pop(0)
        if isinstance(answer, OSError):
            raise answer
        return answer[0], json.dumps(answer[1]).encode()

    @property
    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    out = tmp_path / "dist" / "db-admin"
    out.mkdir(parents=True)
    (out / "agent.yaml").write_text("kind: Agent\nmetadata: {name: db-admin}\n")
    (out / "agentrelease.yaml").write_text("kind: AgentRelease\nspec: {agent: db-admin}\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def environment(**changes) -> dict[str, str]:
    return {
        "REGISTRY_URL": "https://registry.example/",
        "REGISTRY_AUDIENCE": "agent-registry-reports",
        "AGENT": "db-admin",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://actions.example/token?api=1",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-credential",
        **changes,
    }


def run(fake: Fake, **changes):
    out, err = io.StringIO(), io.StringIO()
    code = report.run(
        environment(**changes), http=fake.http, sleep=fake.slept.append, out=out, err=err
    )
    return code, out.getvalue(), err.getvalue()


def test_a_200_exits_zero_and_posts_the_published_files_and_the_suite(repo):
    (repo / "agents" / "db-admin" / "evaluation").mkdir(parents=True)
    (repo / "agents/db-admin/evaluation/suite.yaml").write_text(yaml.safe_dump(SUITE))
    fake = Fake((200, {**GOOD, "created": True}))

    code, out, _ = run(fake)

    assert code == 0
    assert "agent:kubani:tenant-test:db-admin" in out and "revision 3" in out
    ((_, url, headers, body),) = fake.posts
    sent = json.loads(body)
    assert url == "https://registry.example/v1/reports/releases"
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert fake.calls[0][1].endswith("&audience=agent-registry-reports")
    assert sent["agent"]["kind"] == "Agent" and sent["agentRelease"]["kind"] == "AgentRelease"
    assert sent["evaluation"] == {"suite": SUITE}
    assert set(sent["lineage"]["checksums"]) == {"agent.yaml", "agentrelease.yaml"}


def test_without_a_suite_file_the_report_has_no_evaluation(repo):
    fake = Fake((200, {**GOOD, "created": False}))

    assert run(fake)[0] == 0
    assert "evaluation" not in json.loads(fake.posts[0][3])


def test_wrong_path_exits_zero_and_says_so(repo):
    fake = Fake((409, {"error": {"code": "wrong-path", "message": "pull request path"}}))

    code, out, _ = run(fake)

    assert code == 0
    assert "pull request path" in out


@pytest.mark.parametrize(
    "answer",
    [
        (403, {"error": {"code": "refused", "message": "not trusted"}}),
        (409, {"error": {"code": "in-recovery", "message": "recovering"}}),
        (404, {"error": {"code": "not-found", "message": "no deployment"}}),
    ],
)
def test_any_other_answer_fails_and_prints_the_code_and_message(repo, answer):
    code, _, err = run(Fake(answer))

    error = answer[1]["error"]
    assert code != 0
    assert error["code"] in err and error["message"] in err


def test_without_registry_url_nothing_is_reported_and_the_action_does_not_run_the_step(repo):
    fake = Fake()
    steps = yaml.safe_load((ACTION / "action.yml").read_text())["runs"]["steps"]

    code, _, _ = run(fake, REGISTRY_URL="")

    assert code == 0 and fake.calls == []
    assert steps[-1]["if"] == "inputs.registry-url != ''"
    assert "report.py" in steps[-1]["run"]


def test_a_503_is_retried_and_a_network_error_too(repo):
    fake = Fake(
        (503, {"error": {"code": "unavailable", "message": "down"}}),
        OSError("connection reset"),
        (200, {**GOOD, "created": True}),
    )

    code, _, _ = run(fake)

    assert code == 0
    assert len(fake.posts) == 3 and fake.slept == [2, 4]


def test_the_retries_stop_after_five_and_fail(repo):
    fake = Fake(*[(503, {"error": {"code": "unavailable", "message": "down"}})] * 6)

    code, _, err = run(fake)

    assert code != 0 and "unavailable" in err
    assert len(fake.posts) == 6 and sum(fake.slept) == 60


def test_the_token_is_never_printed(repo):
    for answer in (
        (200, {**GOOD, "created": True}),
        (403, {"error": {"code": "refused", "message": "no"}}),
        (409, {"error": {"code": "wrong-path", "message": "pr"}}),
    ):
        _, out, err = run(Fake(answer))

        assert SECRET not in out + err
        assert "request-credential" not in out + err
