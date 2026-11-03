"""Phase B conformance harness (spec 15): runs conformance tests against a live tenant through
the Session Runner and writes a JSON report.

Each sandbox-side test is one session of the conformance agent (zetra-agents
`agents/conformance`): the task names one probe tool and its arguments, the agent calls it
and reports the result, and the harness reads the committed boundaries back from Temporal
history. Evidence outside the sandbox comes from the Agent Policy Service's JSON log, read
through the Kubernetes API. Nothing here is a Temporal worker: the harness only submits
workflows and reads their history, so it runs as a one-shot Job with the runner image.

Tests covered so far (the Phase B gate is 22; the rest are added batch by batch):

- T4: a sandbox obtains an agent token. The first model call of any session goes through
  the gateway token route, so the task is `probe_files`, whose result reports the claims of
  the cached token. The APS log must show "token issued" for exactly that pod.
- T5: the token route refuses every other grant, client or assertion before the IdP sees it.
  Cases that a sandbox can express are posted from inside one (`probe_http`); the two that
  need a real projected token from a non-sandbox pod are posted by the harness itself.

Reports never carry token material: every string is scrubbed with the runner's token patterns
before it is written (spec 13.6).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError
from temporalio.converter import DataConverter

from runner.activities import pod_name
from runner.submit import submit
from runner.types import TOKEN_PATTERNS, BoundaryOut, SessionInput, SessionResult

APS_NAMESPACE = "agent-platform"
APS_LABEL = "app.kubernetes.io/name=agent-policy-service"
APS_CONTAINER = "agent-policy-service"
APS_MESSAGES = ("token issued", "token route refused", "idp issued an unacceptable agent token")
TOKEN_HOST = "token.agents.internal"
TOKEN_PATH = "/v1/token"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
GRANT_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
DEFAULT_APS_URL = "http://agent-policy-service.agent-platform.svc.cluster.local:9002"
DEFAULT_ISSUER_TOKEN_PATH = "/var/run/secrets/issuer-audience/token"
DEFAULT_APS_TOKEN_PATH = "/var/run/secrets/agent-policy-service/token"
SESSION_TIMEOUT = 900.0
# The probe reports {"status": 403, ...}; the model is told to repeat the result as JSON.
STATUS_JSON = re.compile(r"[\"']?status[\"']?\s*[:=]\s*[\"']?([1-5][0-9]{2})")
STATUS_WORD = re.compile(r"\b([45][0-9]{2})\b")
TESTS = ("T4", "T5")


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def scrub(value: Any) -> Any:
    """Replace anything that looks like a token in every string of a JSON structure."""
    if isinstance(value, str):
        for p in TOKEN_PATTERNS:
            value = p.sub("[redacted]", value)
        return value
    if isinstance(value, dict):
        return {scrub(str(k)): scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    return value


def client_id(tenant: str, agent: str) -> str:
    return f"agent-{tenant}-{agent}"


# --- test definitions -------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxCase:
    """One session of the conformance agent and what its probe must report."""

    name: str
    tool: str
    args: dict[str, str]
    expect_status: int | None = None

    def task(self) -> dict:
        return {"tool": self.tool, "args": self.args}


@dataclass(frozen=True)
class DirectCase:
    """One POST to the APS token route from the harness pod (never a sandbox)."""

    name: str
    assertion_path: str  # projected token the harness presents as client_assertion
    expect_status: int


def t4_case() -> SandboxCase:
    return SandboxCase("token_claims_in_sandbox", "probe_files", {})


def t5_sandbox_cases(tenant: str, agent: str) -> list[SandboxCase]:
    cid = client_id(tenant, agent)
    ok = {
        "grant_type": "client_credentials",
        "client_id": cid,
        "client_assertion_type": ASSERTION_TYPE,
        "client_assertion": "not-a-jwt",  # the APS answers before the IdP sees the form
        "scope": "agent-gateway",
    }
    forms: list[tuple[str, dict, int]] = [
        (
            "password_grant",
            {"grant_type": "password", "client_id": "agent-cli", "username": "a", "password": "x"},
            400,
        ),
        ("token_exchange_grant", {**ok, "grant_type": GRANT_TOKEN_EXCHANGE}, 400),
        (
            "client_secret",
            {k: v for k, v in ok.items() if k != "client_assertion"} | {"client_secret": "s"},
            401,
        ),
        ("non_jwt_assertion", ok, 401),
        ("extra_field", {**ok, "audience": "x"}, 400),
        ("duplicate_field", {**ok, "client_id": [cid, cid]}, 400),
    ]
    return [
        SandboxCase(
            name,
            "probe_http",
            {
                "host_header": TOKEN_HOST,
                "path": TOKEN_PATH,
                "auth": "none",
                "form": json.dumps(form, separators=(",", ":")),
            },
            status,
        )
        for name, form, status in forms
    ]


def t5_direct_cases(issuer_token_path: str, aps_token_path: str) -> list[DirectCase]:
    return [
        # A valid projected token of a non-sandbox ServiceAccount: reviewed, then refused.
        DirectCase("non_sandbox_pod", issuer_token_path, 403),
        # A projected token for another audience never authenticates as an assertion.
        DirectCase("wrong_audience", aps_token_path, 401),
    ]


# --- evidence ---------------------------------------------------------------------------------


@dataclass
class SessionRun:
    session_id: str
    pod: str
    outcome: str
    boundaries: list[dict] = field(default_factory=list)
    final_summary: str = ""
    started: str = ""
    finished: str = ""

    def tool_boundaries(self, tool: str) -> list[dict]:
        return [b for b in self.boundaries if b["kind"] == "tool" and b["tool"] == tool]


async def boundaries_from_events(events, converter: DataConverter) -> list[BoundaryOut]:
    """The committed run_boundary results of a session, decoded with the tenant codec."""
    scheduled: dict[int, str] = {}
    out: list[BoundaryOut] = []
    for ev in events:
        if ev.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            scheduled[ev.event_id] = ev.activity_task_scheduled_event_attributes.activity_type.name
        elif ev.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            attrs = ev.activity_task_completed_event_attributes
            if scheduled.get(attrs.scheduled_event_id) != "run_boundary":
                continue
            [doc] = await converter.decode(list(attrs.result.payloads), [dict])
            out.append(BoundaryOut.parse_strict(doc))
    return out


def parse_aps_log(text: str) -> list[dict]:
    """The APS writes one slog JSON document per line; keep the token-route events."""
    lines = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if isinstance(doc, dict) and doc.get("msg") in APS_MESSAGES:
            lines.append(doc)
    return lines


def status_from_summary(summary: str) -> int | None:
    m = STATUS_JSON.search(summary) or STATUS_WORD.search(summary)
    return int(m.group(1)) if m else None


# --- evaluation (pure, so the pass criteria are unit-testable) --------------------------------


def evaluate_t4(run: SessionRun, aps_lines: list[dict], tenant: str) -> dict:
    issued = [
        line
        for line in aps_lines
        if line.get("msg") == "token issued"
        and line.get("pod") == run.pod
        and line.get("namespace") == tenant
    ]
    probes = run.tool_boundaries("probe_files")
    checks = {
        "session_completed": run.outcome == "COMPLETED",
        "probe_files_ran": bool(probes),
        "token_issued_for_pod": len(issued) >= 1,
        "one_token_per_sandbox": len(issued) == 1,
        "claims_reported": "agent-gateway" in run.final_summary,
    }
    required = ("session_completed", "probe_files_ran", "token_issued_for_pod")
    return {
        "result": "pass" if all(checks[k] for k in required) else "fail",
        "checks": checks,
        "evidence": {
            "session": asdict(run),
            "token_issued": [
                {k: v for k, v in line.items() if k in ("time", "agent_id", "pod", "namespace")}
                for line in issued
            ],
        },
    }


def evaluate_t5_sandbox(case: SandboxCase, run: SessionRun, aps_lines: list[dict]) -> dict:
    probes = run.tool_boundaries("probe_http")
    status = status_from_summary(run.final_summary)
    refused = [line for line in aps_lines if line.get("msg") == "token route refused"]
    forwarded = [line for line in aps_lines if line.get("msg") == "token issued"]
    checks = {
        "session_completed": run.outcome == "COMPLETED",
        "probe_http_ran": bool(probes),
        "status_reported": status is not None,
        "status_expected": status == case.expect_status,
        "aps_logged_refusal": len(refused) >= 1,
    }
    required = ("session_completed", "probe_http_ran", "status_expected", "aps_logged_refusal")
    return {
        "case": case.name,
        "expected_status": case.expect_status,
        "observed_status": status,
        "result": "pass" if all(checks[k] for k in required) else "fail",
        "checks": checks,
        "evidence": {
            "session": asdict(run),
            "refusals": [
                {k: v for k, v in line.items() if k in ("time", "reason", "field")}
                for line in refused
            ],
            # The sandbox's own token for its model call is the only issuance expected here.
            "issued_during_case": len(forwarded),
        },
    }


def evaluate_t5_direct(case: DirectCase, status: int | None, body: str, error: str | None) -> dict:
    checks = {"status_expected": status == case.expect_status, "no_token_in_reply": True}
    if "access_token" in body:
        checks["no_token_in_reply"] = False
    return {
        "case": case.name,
        "expected_status": case.expect_status,
        "observed_status": status,
        "result": "pass" if all(checks.values()) and error is None else "fail",
        "checks": checks,
        "evidence": {"body": scrub(body[:512]), "error": error},
    }


def summarize(tests: list[dict]) -> str:
    results = {t["result"] for t in tests}
    if "error" in results:
        return "error"
    return "fail" if "fail" in results else "pass"


# --- the harness ------------------------------------------------------------------------------


def post_token_form(aps_url: str, form: dict[str, str], timeout: float = 10.0) -> tuple[int, str]:
    """POST to the APS token route directly (not through the gateway) and return status, body."""
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(aps_url.rstrip("/") + TOKEN_PATH, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(1 << 16).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read(1 << 16).decode("utf-8", "replace")
        e.close()
        return e.code, body


class Harness:
    def __init__(
        self,
        client: Client,
        tenant: str,
        agent: str,
        release: str,
        aps_logs: Callable[[float], Awaitable[str]],
        aps_url: str = DEFAULT_APS_URL,
        issuer_token_path: str = DEFAULT_ISSUER_TOKEN_PATH,
        aps_token_path: str = DEFAULT_APS_TOKEN_PATH,
        session_timeout: float = SESSION_TIMEOUT,
        post: Callable[[str, dict[str, str]], tuple[int, str]] = post_token_form,
    ) -> None:
        self.client = client
        self.tenant, self.agent, self.release = tenant, agent, release
        self.aps_logs = aps_logs  # since-seconds -> raw log text of every APS pod
        self.aps_url = aps_url
        self.issuer_token_path = issuer_token_path
        self.aps_token_path = aps_token_path
        self.session_timeout = session_timeout
        self.post = post

    async def run_session(self, case: SandboxCase) -> SessionRun:
        session_id = str(uuid.uuid4())
        inp = SessionInput(self.tenant, self.agent, self.release, session_id, None, case.task())
        run = SessionRun(session_id, pod_name(session_id, 1), "SUBMITTED", started=utcnow())
        handle = await submit(self.client, inp)
        try:
            result: SessionResult = await asyncio.wait_for(handle.result(), self.session_timeout)
            run.outcome = result.outcome
        except WorkflowFailureError as e:
            run.outcome = f"FAILED:{type(e.cause).__name__}"
        except TimeoutError:
            run.outcome = "HARNESS_TIMEOUT"
        run.finished = utcnow()
        events = [ev async for ev in handle.fetch_history_events()]
        try:
            boundaries = await boundaries_from_events(events, self.client.data_converter)
        except ValueError as e:
            run.outcome = f"{run.outcome}:undecodable_history:{type(e).__name__}"
            boundaries = []
        run.boundaries = [asdict(b) for b in boundaries]
        for b in reversed(boundaries):
            if b.kind == "model" and b.summary:
                run.final_summary = b.summary
                break
        return run

    async def aps_lines_since(self, started: float) -> list[dict]:
        return parse_aps_log(await self.aps_logs(max(1.0, time.time() - started + 5)))

    async def t4(self) -> dict:
        started = time.time()
        case = t4_case()
        run = await self.run_session(case)
        lines = await self.aps_lines_since(started)
        return {
            "id": "T4",
            "name": "sandbox token issuance",
            **evaluate_t4(run, lines, self.tenant),
        }

    async def t5(self) -> dict:
        cases = []
        for case in t5_sandbox_cases(self.tenant, self.agent):
            started = time.time()
            run = await self.run_session(case)
            lines = await self.aps_lines_since(started)
            cases.append(evaluate_t5_sandbox(case, run, lines))
        cid = client_id(self.tenant, self.agent)
        for direct in t5_direct_cases(self.issuer_token_path, self.aps_token_path):
            try:
                assertion = Path(direct.assertion_path).read_text(encoding="utf-8").strip()
            except OSError as e:
                cases.append(
                    {
                        "case": direct.name,
                        "expected_status": direct.expect_status,
                        "observed_status": None,
                        "result": "error",
                        "checks": {},
                        "evidence": {"error": f"assertion unreadable: {type(e).__name__}"},
                    }
                )
                continue
            form = {
                "grant_type": "client_credentials",
                "client_id": cid,
                "client_assertion_type": ASSERTION_TYPE,
                "client_assertion": assertion,
                "scope": "agent-gateway",
            }
            try:
                status, body = self.post(self.aps_url, form)
                error = None
            except (OSError, urllib.error.URLError) as e:
                status, body, error = None, "", type(e).__name__
            cases.append(evaluate_t5_direct(direct, status, body, error))
        return {
            "id": "T5",
            "name": "token route refusals",
            "result": summarize(cases),
            "cases": cases,
        }

    async def run(self, tests: list[str]) -> dict:
        started = utcnow()
        results = []
        for t in tests:
            try:
                results.append(await getattr(self, t.lower())())
            except Exception as e:  # noqa: BLE001 - one broken test must not hide the others
                results.append({"id": t, "result": "error", "error": type(e).__name__})
        return scrub(
            {
                "harness": "agent-platform-runner conformance",
                "tenant": self.tenant,
                "agent": self.agent,
                "release": self.release,
                "started": started,
                "finished": utcnow(),
                "result": summarize(results),
                "tests": results,
            }
        )


# --- cluster wiring --------------------------------------------------------------------------


def aps_log_reader(core) -> Callable[[float], Awaitable[str]]:
    """Read the token-route events of every APS pod from the last `since` seconds."""

    async def read(since: float) -> str:
        def blocking() -> str:
            pods = core.list_namespaced_pod(APS_NAMESPACE, label_selector=APS_LABEL)
            chunks = []
            for pod in pods.items:
                try:
                    chunks.append(
                        core.read_namespaced_pod_log(
                            pod.metadata.name,
                            APS_NAMESPACE,
                            container=APS_CONTAINER,
                            since_seconds=int(since) + 1,
                        )
                    )
                except Exception as e:  # noqa: BLE001 - a pod mid-restart has no log yet
                    chunks.append(
                        json.dumps(
                            {
                                "msg": "log unavailable",
                                "pod": pod.metadata.name,
                                "error": type(e).__name__,
                            }
                        )
                    )
            return "\n".join(chunks)

        return await asyncio.to_thread(blocking)

    return read


async def run_in_cluster(args: argparse.Namespace) -> dict:
    from kubernetes import client as k8s
    from kubernetes import config

    from runner.worker import connect

    config.load_incluster_config()
    core = k8s.CoreV1Api()
    harness = Harness(
        await connect(args.tenant),
        args.tenant,
        args.agent,
        args.release,
        aps_log_reader(core),
        aps_url=os.environ.get("APS_URL", DEFAULT_APS_URL),
        issuer_token_path=os.environ.get("ISSUER_TOKEN_PATH", DEFAULT_ISSUER_TOKEN_PATH),
        aps_token_path=os.environ.get("APS_TOKEN_PATH", DEFAULT_APS_TOKEN_PATH),
        session_timeout=args.session_timeout,
    )
    return await harness.run(args.tests)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase B conformance harness")
    parser.add_argument("--tenant", default=os.environ.get("TENANT", "tenant-test"))
    parser.add_argument("--agent", default="conformance")
    parser.add_argument(
        "--release", required=True, help="AgentRelease name of the conformance agent"
    )
    parser.add_argument("--tests", default=",".join(TESTS), help="comma-separated, e.g. T4,T5")
    parser.add_argument("--report", type=Path, default=None, help="write the JSON report here too")
    parser.add_argument("--session-timeout", type=float, default=SESSION_TIMEOUT)
    args = parser.parse_args(argv)
    args.tests = [t.strip().upper() for t in args.tests.split(",") if t.strip()]
    unknown = [t for t in args.tests if t not in TESTS]
    if unknown:
        parser.error(f"unknown tests {unknown}; known: {', '.join(TESTS)}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = asyncio.run(run_in_cluster(args))
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        args.report.write_text(text + "\n", encoding="utf-8")
    print(text)
    return {"pass": 0, "fail": 1, "error": 2}[report["result"]]


if __name__ == "__main__":
    sys.exit(main())
