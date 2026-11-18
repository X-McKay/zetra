"""Run one claimed evaluation's suite to a verdict (delivery.md section 6.3)."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .sessions import SessionError, SessionsPort

CASE_POLL_SECONDS = 5.0
START_TRIES = 5
START_GAP_SECONDS = 15.0  # five tries leave four gaps: about a minute


class SuiteError(Exception):
    """The suite cannot be run to a verdict; the verdict is `error`."""

    def __init__(self, summary: str, case: dict | None = None) -> None:
        super().__init__(summary)
        self.case = case


@dataclass
class Runtime:
    """What a case needs besides the evaluation: the interface, the clock, and sleeping."""

    sessions: SessionsPort
    clock: Callable[[], float]
    sleep: Callable[[float], None]


def evaluation_id(ev: dict) -> str:
    return f"{ev['deployment']}@{ev['revision']}#{ev['attempt']}"


def session_id(ev: dict, case_id: str) -> str:
    """The same evaluation and case always give the same id, so a rerun finds the session."""
    digest = hashlib.sha256(f"{evaluation_id(ev)}/{case_id}".encode()).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def report_text(report: Any) -> str:
    """A string is checked as it is; any other JSON value as its compact JSON text."""
    if report is None:
        return ""
    if isinstance(report, str):
        return report
    return json.dumps(report, separators=(",", ":"), ensure_ascii=False)


def check_report(expect: dict, text: str) -> list[str]:
    """The checks of `expect` that do not hold, each as a short phrase."""
    failed = []
    for needle in expect.get("report_contains", []):
        if needle not in text:
            failed.append(f"report_contains: {json.dumps(needle)} not found")
    for pattern in expect.get("report_matches", []):
        if not re.search(pattern, text):
            failed.append(f"report_matches: {json.dumps(pattern)} not found")
    for needle in expect.get("report_excludes", []):
        if needle in text:
            failed.append(f"report_excludes: {json.dumps(needle)} found")
    return failed


def _retry(fn: Callable[[], dict], sleep: Callable[[float], None]) -> dict:
    """Try again while the interface cannot be reached or fails on its side; 4xx ends it."""
    for attempt in range(START_TRIES):
        try:
            return fn()
        except SessionError as exc:
            if not exc.transient or attempt == START_TRIES - 1:
                raise
            sleep(START_GAP_SECONDS)
    raise AssertionError("unreachable")


def _error_summary(case_id: str, exc: SessionError) -> str:
    what = f"{exc.code}: {exc}" if exc.code else str(exc)
    return f"case {case_id}: the session interface answered {what}"[:500]


def run_case(ev: dict, case: dict, timeout: float, run: Runtime) -> dict:
    """One case to its result entry. Raises SuiteError when no verdict can come of it."""
    sid = session_id(ev, case["id"])
    agent, tenant = ev["agent"], ev["tenant"]
    failure = {"id": case["id"], "passed": False, "sessionId": sid, "outcome": "error"}
    deadline = run.clock() + timeout
    body = {
        "agent": agent,
        "release": ev["release"],
        "task": {"request": case["task"]},  # the shape `just agent run --task` sends
        "sessionId": sid,
        "purpose": "evaluation",
    }
    try:
        _retry(lambda: run.sessions.start(tenant, body), run.sleep)
        while True:
            session = _retry(lambda: run.sessions.read(tenant, agent, sid), run.sleep)
            if session.get("state") == "finished":
                break
            if run.clock() >= deadline:
                _cancel(run, ev, sid, case["id"])
                raise SuiteError(
                    f"case {case['id']} ran past {timeout:g} seconds and was cancelled",
                    {**failure, "outcome": "timed-out", "detail": "past timeout_seconds"},
                )
            run.sleep(CASE_POLL_SECONDS)
    except SessionError as exc:
        summary = _error_summary(case["id"], exc)
        raise SuiteError(summary, {**failure, "detail": summary}) from None

    outcome = str(session.get("outcome"))
    expect = case.get("expect") or {}
    wanted = expect.get("outcome", "completed")
    problems = []
    if outcome != wanted:
        problems.append(f"outcome: {outcome}, wanted {wanted}")
    text = report_text(session.get("report"))
    if session.get("report") is None and session.get("reportProblem"):
        problems.append(f"report: {session['reportProblem']}")
    problems += check_report(expect, text)
    return {
        "id": case["id"],
        "passed": not problems,
        "sessionId": sid,
        "outcome": outcome,
        "detail": "; ".join(problems)[:1000],
    }


def _cancel(run: Runtime, ev: dict, sid: str, case_id: str) -> None:
    try:
        run.sessions.cancel(
            ev["tenant"],
            ev["agent"],
            sid,
            f"evaluation {evaluation_id(ev)} case {case_id} timed out",
        )
    except SessionError:
        pass  # the verdict is error either way; the session's own time limit ends it


def run_suite(ev: dict, run: Runtime) -> dict:
    """The verdict, summary and cases of the evaluation, without the evaluator's name."""
    suite = ev["suite"]
    cases = suite["cases"]
    timeout = suite["timeout_seconds"]
    needed = suite.get("acceptance", {}).get("min_passed", "all")
    required = len(cases) if needed == "all" else int(needed)
    results: list[dict] = []
    try:
        for case in cases:
            results.append(run_case(ev, case, timeout, run))
    except SuiteError as exc:
        if exc.case:
            results.append(exc.case)
        return {"verdict": "error", "summary": str(exc), "cases": results}
    passed = sum(1 for r in results if r["passed"])
    needs = "all" if needed == "all" else str(required)
    if passed >= required:
        verdict, summary = "passed", f"{passed} of {len(cases)} cases passed"
    else:
        verdict = "failed"
        summary = f"{passed} of {len(cases)} cases passed; acceptance needs {needs}"
    return {"verdict": verdict, "summary": summary, "cases": results}
