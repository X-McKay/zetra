"""In-memory fakes for the two ports, and a clock that sleeping moves."""

from __future__ import annotations

from registry_evaluator.registry import RegistryError
from registry_evaluator.sessions import SessionError


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def evaluation(cases, *, timeout=60, min_passed="all", attempt=1) -> dict:
    return {
        "deployment": "agent:kubani:tenant-test:db-admin",
        "revision": 3,
        "attempt": attempt,
        "agent": "db-admin",
        "tenant": "tenant-test",
        "namespace": "tenant-test",
        "release": "db-admin-2026-11-20-1a2b3c4d",
        "suite": {
            "schema_version": 1,
            "timeout_seconds": timeout,
            "acceptance": {"min_passed": min_passed},
            "cases": cases,
        },
        "suiteDigest": "sha256:0",
        "claimExpiresAt": "2026-11-20T10:15:00Z",
    }


def case(case_id, **expect) -> dict:
    return {"id": case_id, "task": f"do {case_id}", "expect": expect}


class FakeRegistry:
    def __init__(self, *evaluations: dict) -> None:
        self.waiting = list(evaluations)
        self.claims: list[tuple[str, int]] = []
        self.results: list[dict] = []
        self.result_errors: list[RegistryError] = []  # raised one per post, oldest first
        self.answer: dict = {"recorded": True, "phase": "pending-approval"}

    def claim(self, evaluator, lease_seconds):
        self.claims.append((evaluator, lease_seconds))
        return self.waiting.pop(0) if self.waiting else None

    def result(self, deployment, revision, attempt, body):
        if self.result_errors:
            raise self.result_errors.pop(0)
        self.results.append(
            {"deployment": deployment, "revision": revision, "attempt": attempt, **body}
        )
        return self.answer


class FakeSessions:
    """The session interface: one session per id; a start with a known id returns that one.

    `script` maps a task to (outcome, report, polls): the session reports `running` for
    `polls` reads and is then finished. polls=None never finishes.
    """

    def __init__(self, script: dict | None = None) -> None:
        self.script = script or {}
        self.sessions: dict[str, dict] = {}
        self.starts: list[dict] = []
        self.cancelled: list[tuple[str, str]] = []
        self.refuse: SessionError | None = None
        self.down = 0  # reads and starts that fail as unreachable before it answers

    def _maybe_down(self) -> None:
        if self.down:
            self.down -= 1
            raise SessionError("POST: ConnectError")

    def start(self, tenant, body):
        self._maybe_down()
        self.starts.append(body)
        if self.refuse:
            raise self.refuse
        assert body["purpose"] == "evaluation" and tenant == "tenant-test"
        sid = body["sessionId"]
        created = sid not in self.sessions
        if created:
            self.sessions[sid] = {"task": body["task"]["request"], "reads": 0}
        return {"sessionId": sid, "state": "running", "created": created}

    def read(self, tenant, agent, session_id):
        self._maybe_down()
        s = self.sessions[session_id]
        s["reads"] += 1
        outcome, report, polls = self.script.get(s["task"], ("completed", "ok", 0))
        if polls is None or s["reads"] <= polls or session_id in (c[1] for c in self.cancelled):
            return {"sessionId": session_id, "state": "running", "outcome": None, "report": None}
        return {
            "sessionId": session_id,
            "state": "finished",
            "outcome": outcome,
            "report": report,
            "reportProblem": None,
        }

    def cancel(self, tenant, agent, session_id, reason):
        self.cancelled.append((agent, session_id))
        return {"sessionId": session_id, "cancelRequested": True}
