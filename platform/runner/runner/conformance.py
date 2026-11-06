"""Phase B conformance harness (spec 15): runs conformance tests against a live tenant through
the Session Runner and writes a JSON report.

Each sandbox-side test is one session of the conformance agent (zetra-agents
`agents/conformance`): the task names one probe tool and its arguments, the agent calls it
and reports the result, and the harness reads the committed boundaries back from Temporal
history. Evidence outside the sandbox comes from the Agent Policy Service's JSON log, read
through the Kubernetes API. Nothing here is a Temporal worker: the harness only submits
workflows and reads their history, so it runs as a one-shot Job with the runner image.

Tests covered so far (the Phase B gate is 22; the rest are added batch by batch):

- T4: a sandbox obtains an agent token. Read the cached token's decoded allowlisted claims
  from probe_files in the verified committed snapshot, require exact spec claims, and join
  its JTI to APS issuance for the current sandbox generation.
- T5: the token route refuses every other grant, client or assertion before the IdP sees it.
  Sandbox-expressible cases use probe_http. The harness also presents its issuer-audience
  token, its API-audience token authenticated by SelfSubjectReview, and a genuine signed,
  unexpired human fixture. Missing or unverifiable fixtures cannot establish a pass.
- T6: the harness calls the model engine directly without a credential and with a mounted
  raw backend key. Only HTTP statuses are retained: expected 401 then 200.
- T7: a normal conformance session completes a model call through the gateway. The model
  server's capture must show a static backend-key match and no agent JWT, the chat-completion
  path and status 200. Committed boundaries and gateway logs must both show nonzero usage.
- T8: forged `x-agent-*` request headers never become the identity the APS acts on. One
  `probe_mcp` session calls an allowed tool with forged headers; the APS decision for that
  call must carry the sandbox's own agent, session and principal, and no forged value may
  appear anywhere in the decision log. (Spec 15 also wants a capture at the MCP server; the
  backends here log no headers, so that half is recorded as not captured.)
- T19: no decision is cached. One fixed token covers 50 allowed/forbidden MCP cycles within
  ten seconds, followed by two allowed/forbidden model-route cycles. Actual HTTP statuses
  come from the raw tool result in the accepted snapshot, never the model's summary. Trace
  ids join each request to one distinct APS decision; every decision must name that token.
- T11: an agent-mode session's upstream token. The capture proxy in front of the kubernetes
  MCP server (zetra platform/capture) records what the server received; every bearer for the
  session must carry the agent's own subject (the inbound token's `sub`, read from the
  gateway's access log) and no `act`, and the call must be allowed by the agent's role.
- T12: the agent access token never reaches the MCP server: no captured `jti` equals the
  `jti` the APS issued to the session's pod, and every captured `jti` is one the APS minted by
  exchange for that pod. (The native-exchange configuration of spec 8.11 is not deployed.)
- T8 also reads the capture: the server received the APS-set `x-agent-*` values for the
  session, none of the forged ones, and no `x-agent-decision-reason`.
- T26: token binding. Session 1 (`probe_token_stash`) writes its agent token into the
  workspace, which the runner snapshots unredacted into the artifact store. Session 2 of the
  same agent and a session of agent B are *seeded* from that snapshot (`SessionInput.seed`),
  so the token is in their workspaces and in no session input (10.2 forbids that), and each
  presents it on the MCP route with `probe_http(auth="stash")`. Expected: 403
  `deny.token_bound_elsewhere` for the same agent, 403 `deny.pod_agent_mismatch` for agent
  B. The harness reads the snapshot from the store (mounted read-only) only to compare the
  token by `jti` (15.4); the token goes nowhere else.

Reports never carry token material: every string is scrubbed with the runner's token patterns
before it is written (spec 13.6).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import sys
import tarfile
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
from runner.token_conformance import (
    allowlisted_claims,
    api_assertion_identity,
    load_jwks,
    read_assertion,
    verify_api_fixture,
    verify_human_fixture,
)
from runner.types import TOKEN_PATTERNS, BoundaryOut, SessionInput, SessionResult

APS_NAMESPACE = "agent-platform"
APS_LABEL = "app.kubernetes.io/name=agent-policy-service"
APS_CONTAINER = "agent-policy-service"
APS_MESSAGES = ("token issued", "token route refused", "idp issued an unacceptable agent token")
LOG_UNAVAILABLE = "log unavailable"  # the harness's own marker for an APS pod it could not read
LOG_READ_ATTEMPTS = 3
TOKEN_HOST = "token.agents.internal"
TOKEN_PATH = "/v1/token"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
GRANT_TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
DEFAULT_APS_URL = "http://agent-policy-service.agent-platform.svc.cluster.local:9002"
DEFAULT_ISSUER_TOKEN_PATH = "/var/run/secrets/issuer-audience/token"
DEFAULT_API_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
DEFAULT_API_SUBJECT = "system:serviceaccount:agent-platform:conformance-harness"
SESSION_TIMEOUT = 900.0
# The MCP route (spec 8.8, 9.3): the conformance Agent's bundle admits one kubernetes tool;
# pods_get is in the gateway's role policy for the backend but not in the bundle, so the APS
# must deny it on every request.
MCP_ROUTE = "mcp"
MCP_HOST = "mcp.agents.internal"
MCP_BACKEND = "kubernetes"
MCP_ALLOWED_TOOL = "pods_list_in_namespace"
MCP_FORBIDDEN_TOOL = "pods_get"
DENY_ROUTE = "deny.route_not_permitted"
T19_ROUNDS = 50
T19_MODEL_ROUNDS = 2
# T8: values a sandbox might forge; none may survive into a decision or reach an upstream.
FORGED_HEADERS = {
    "x-agent-id": "tenant-test/other",
    "x-agent-principal": "mallory",
    "x-agent-session-id": "0000",
    "x-agent-decision-reason": "allow",
}
# The probe reports {"status": 403, ...}; the model is told to repeat the result as JSON.
STATUS_JSON = re.compile(r"[\"']?status[\"']?\s*[:=]\s*[\"']?([1-5][0-9]{2})")
STATUS_WORD = re.compile(r"\b([45][0-9]{2})\b")
TESTS = ("T4", "T5", "T6", "T7", "T8", "T11", "T12", "T19", "T26")
ADMISSION_TESTS = ("T9a", "T9b", "T22")
BUDGET_TESTS = ("T13", "T13b")
DEFAULT_BUDGET_TOKEN_PATH = "/var/run/secrets/agent-policy-service/token"
PROTOCOL_TESTS = ("T25",)
REVOCATION_TESTS = ("T15", "T20")
DEFAULT_ARTIFACT_DIR = "/var/lib/agent-artifacts"
TOKEN_STASH_MEMBER = "workspace/t26/token"  # what probe_token_stash writes, as the tar names it
DENY_BOUND = "deny.token_bound_elsewhere"
DENY_MISMATCH = "deny.pod_agent_mismatch"
CAPTURE_NAMESPACE, CAPTURE_LABEL, CAPTURE_CONTAINER = (
    "mcp",
    "app.kubernetes.io/name=kubernetes-mcp",
    "capture",
)
MODEL_CAPTURE_NAMESPACE, MODEL_CAPTURE_LABEL, MODEL_CAPTURE_CONTAINER = (
    "vllm",
    "app=vllm",
    "capture",
)
MODEL_CHAT_PATH = "/v1/chat/completions"
GATEWAY_MODEL_CHAT_PATH = "/models/llm-main/v1/chat/completions"
GATEWAY_NAMESPACE, GATEWAY_LABEL, GATEWAY_CONTAINER = (
    "ai-gateway",
    "app.kubernetes.io/name=agents",
    None,
)


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


def sandbox_prefix(session_id: str) -> str:
    """Every generation of a session's sandbox is `s-<hash>-g<n>` (activities.pod_name)."""
    return pod_name(session_id, 0)[:-1]


def failure_type(exc: BaseException) -> str:
    """The ApplicationError type behind a workflow failure, else the outermost class name."""
    cause: BaseException | None = exc
    while cause is not None:
        kind = getattr(cause, "type", None)
        if isinstance(kind, str) and kind:
            return kind
        cause = cause.__cause__
    return type(exc).__name__


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


def t8_case(tenant: str) -> SandboxCase:
    return SandboxCase(
        "forged_agent_headers",
        "probe_mcp",
        {
            "tool": MCP_ALLOWED_TOOL,
            "args": json.dumps({"namespace": tenant}, separators=(",", ":")),
            "headers": json.dumps(FORGED_HEADERS, separators=(",", ":")),
        },
    )


def t11_case(tenant: str) -> SandboxCase:
    """One allowed MCP call in agent mode; T11 and T12 read what the server received."""
    return SandboxCase(
        "agent_mode_call",
        "probe_mcp",
        {
            "tool": MCP_ALLOWED_TOOL,
            "args": json.dumps({"namespace": tenant}, separators=(",", ":")),
        },
    )


def t26_stash_case() -> SandboxCase:
    return SandboxCase("token_stash", "probe_token_stash", {})


def t26_present_case(name: str) -> SandboxCase:
    """Present the token another sandbox stashed, on the MCP route (an `initialize`, which the
    APS admits for a sandbox's own token). The session is seeded from the stash session's
    snapshot, so the token is in this sandbox's workspace and never in any session input
    (10.2 forbids that, and the runner enforces it)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    return SandboxCase(
        name,
        "probe_http",
        {
            "host_header": MCP_HOST,
            "path": f"/{MCP_BACKEND}/mcp",
            "auth": "stash",
            "headers": json.dumps(
                {
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                separators=(",", ":"),
            ),
            "body": json.dumps(body, separators=(",", ":")),
        },
        403,
    )


def jwt_jti(token: str) -> str | None:
    """The jti of a compact JWT, read without verification; the APS is what verifies."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        return str(json.loads(payload).get("jti") or "") or None
    except (ValueError, AttributeError):
        return None


def read_stashed_token(artifact_dir: str, snapshot: str) -> str:
    """The token probe_token_stash left in the workspace, from the session's final snapshot in
    the artifact store (FilesystemStore: <root>/<key[:2]>/<key>, a tar of workspace and state)."""
    if len(snapshot) != 64 or any(c not in "0123456789abcdef" for c in snapshot):
        raise ValueError("snapshot key must be a SHA-256 hex digest")
    path = Path(artifact_dir) / snapshot[:2] / snapshot
    with tarfile.open(path, "r:*") as tar:
        member = tar.extractfile(TOKEN_STASH_MEMBER)
        if member is None:
            raise FileNotFoundError(TOKEN_STASH_MEMBER)
        return member.read().decode("utf-8").strip()


def read_t19_probe_result(artifact_dir: str, snapshot: str) -> dict:
    return _read_probe_result(artifact_dir, snapshot, "probe_mcp")


def read_t4_claims(artifact_dir: str, snapshot: str) -> dict:
    probe = _read_probe_result(artifact_dir, snapshot, "probe_files")
    files = probe.get("files")
    if not isinstance(files, list):
        raise ValueError("missing file evidence")
    runtime = [
        item
        for item in files
        if isinstance(item, dict) and item.get("path") == "/sandbox/state/token.json"
    ]
    if len(runtime) != 1:
        raise ValueError("expected one runtime token file")
    claims = runtime[0].get("claims")
    if not isinstance(claims, list) or len(claims) != 1 or not isinstance(claims[0], dict):
        raise ValueError("expected one runtime token")
    return allowlisted_claims(claims[0])


def _read_probe_result(artifact_dir: str, snapshot: str, tool: str) -> dict:
    """Read only the probe's raw tool result from the final accepted, verified snapshot.

    The boundary summary is model prose and may be truncated. The snapshot's state.json
    retains the tool result itself; no prompts, token cache or other state is returned.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot):
        raise ValueError("invalid snapshot key")
    path = Path(artifact_dir) / snapshot[:2] / snapshot
    with path.open("rb") as source:
        digest, size = hashlib.sha256(), 0
        while chunk := source.read(1 << 20):
            size += len(chunk)
            if size > 64 << 20:
                raise ValueError("snapshot too large")
            digest.update(chunk)
        if digest.hexdigest() != snapshot:
            raise ValueError("snapshot digest mismatch")
        source.seek(0)
        with tarfile.open(fileobj=source, mode="r:") as archive:
            members = [m for m in archive if m.name == "state/state.json"]
            if len(members) != 1 or not members[0].isfile() or members[0].size > 1 << 20:
                raise ValueError("missing or invalid snapshot state")
            state = json.load(archive.extractfile(members[0]))
    calls = set()
    results = []
    for message in state["messages"]:
        if message.get("role") == "assistant":
            calls.update(
                call["id"]
                for call in message.get("tool_calls", [])
                if call.get("function", {}).get("name") == tool
            )
        elif message.get("role") == "tool" and message.get("tool_call_id") in calls:
            results.append(json.loads(message["content"]))
    if len(results) != 1 or not isinstance(results[0], dict):
        raise ValueError("expected one probe result")
    return results[0]


def t19_case(tenant: str, rounds: int = T19_ROUNDS) -> SandboxCase:
    return SandboxCase(
        "alternating_tools",
        "probe_mcp",
        {
            "tool": MCP_ALLOWED_TOOL,
            "alt_tool": MCP_FORBIDDEN_TOOL,
            "rounds": str(rounds),
            "model_routes": "true",
            "args": json.dumps({"namespace": tenant}, separators=(",", ":")),
        },
    )


def t5_direct_cases(
    issuer_token_path: str, api_token_path: str, human_token_path: str = ""
) -> list[DirectCase]:
    return [
        # A valid projected token of a non-sandbox ServiceAccount: reviewed, then refused.
        DirectCase("non_sandbox_pod", issuer_token_path, 403),
        DirectCase("api_audience", api_token_path, 401),
        DirectCase("human_token", human_token_path, 401),
    ]


# --- evidence ---------------------------------------------------------------------------------


@dataclass
class SessionRun:
    session_id: str
    pod: str  # the sandbox of the generation that finished (the first, until known)
    outcome: str
    generation: int = 1
    boundaries: list[dict] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)  # decoded activity and workflow failures
    final_summary: str = ""
    started: str = ""
    finished: str = ""
    snapshot: str | None = None  # the final workspace snapshot's key in the artifact store

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


async def failure_message(failure, converter: DataConverter) -> str:
    """The runner's failure converter moves the message into encoded (encrypted) attributes."""
    if failure.HasField("encoded_attributes"):
        try:
            [attrs] = await converter.decode([failure.encoded_attributes], [dict])
            return str(attrs.get("message", ""))
        except Exception as e:  # noqa: BLE001 - never let an undecodable failure hide the rest
            return f"[undecodable: {type(e).__name__}]"
    return failure.message


async def failures_from_events(events, converter: DataConverter) -> list[dict]:
    """Why a session failed: every activity failure or timeout, and the workflow's own
    failure, with the activity name, the ApplicationError type and the decoded message."""
    scheduled: dict[int, str] = {}
    out: list[dict] = []
    for ev in events:
        if ev.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            scheduled[ev.event_id] = ev.activity_task_scheduled_event_attributes.activity_type.name
        elif ev.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED:
            attrs = ev.activity_task_failed_event_attributes
            f = attrs.failure
            info = f.application_failure_info
            out.append(
                {
                    "activity": scheduled.get(attrs.scheduled_event_id, "?"),
                    "type": info.type or "Failure",
                    "non_retryable": bool(info.non_retryable),
                    "message": await failure_message(f, converter),
                }
            )
        elif ev.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT:
            attrs = ev.activity_task_timed_out_event_attributes
            out.append(
                {
                    "activity": scheduled.get(attrs.scheduled_event_id, "?"),
                    "type": "TimedOut",
                    "non_retryable": False,
                    "message": str(attrs.failure.timeout_failure_info.timeout_type),
                }
            )
        elif ev.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED:
            f = ev.workflow_execution_failed_event_attributes.failure
            out.append(
                {
                    "activity": "workflow",
                    "type": f.application_failure_info.type or "Failure",
                    "non_retryable": True,
                    "message": await failure_message(f, converter),
                }
            )
    return out


def parse_aps_log(text: str) -> list[dict]:
    """The APS writes one JSON document per line: slog events on stderr and ext_authz decisions
    (spec 13.5, every line carries a decision_id) on stdout. Keep the token-route events and
    the decisions."""
    lines = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if isinstance(doc, dict) and (
            doc.get("msg") in APS_MESSAGES
            or doc.get("msg") == LOG_UNAVAILABLE
            or "decision_id" in doc
        ):
            lines.append(doc)
    return lines


def unreadable_pods(aps_lines: list[dict]) -> list[str]:
    """APS pods whose log the harness could not read: evidence that may be missing, not a
    platform fact. Run 7 lost T4's `token issued` line this way without a trace."""
    return sorted(
        {str(line.get("pod")) for line in aps_lines if line.get("msg") == LOG_UNAVAILABLE}
    )


def parse_capture_log(text: str) -> list[dict]:
    """The capture proxy writes one JSON record per forwarded request (zetra platform/capture):
    method, path, status, x_agent (every x-agent-* header) and bearer (sub, act_sub, aud, azp,
    jti), and an optional static backend_key_match boolean, never a token or key."""
    out = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if isinstance(doc, dict) and "x_agent" in doc:
            out.append(doc)
    return out


GATEWAY_FIELD = re.compile(r'([A-Za-z0-9_.]+)=("((?:[^"\\]|\\.)*)"|\S*)')


def parse_gateway_log(text: str) -> list[dict]:
    """agentgateway's access log is one text line per request of key=value pairs (values
    with spaces are quoted). Keep the request lines as dicts."""
    out = []
    for raw in text.splitlines():
        if "http.path=" not in raw:
            continue
        doc = {}
        for m in GATEWAY_FIELD.finditer(raw):
            doc[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(2)
        out.append(doc)
    return out


def captures_of(run: SessionRun, records: list[dict]) -> list[dict]:
    """The capture records of this session: the APS sets x-agent-session-id on every upstream
    request, so the server-side record names the session itself."""
    return sorted(
        [r for r in records if r.get("x_agent", {}).get("x-agent-session-id") == run.session_id],
        key=lambda r: str(r.get("ts", "")),
    )


def inbound_subjects(run: SessionRun, gateway_lines: list[dict]) -> set[str]:
    """The `sub` of the inbound agent token on this session's gateway requests."""
    return {
        line["jwt.sub"]
        for line in gateway_lines
        if line.get("agent.session_id") == run.session_id and line.get("jwt.sub")
    }


def mcp_calls_of(run: SessionRun, aps_lines: list[dict]) -> list[dict]:
    """The APS decisions on tools/call requests of this session's sandbox, in time order.

    The log text is one APS pod after another, and a session's requests are spread over the
    replicas, so the lines must be ordered by their timestamp before an alternation can be
    judged (run 7 read a real alternation as broken for this reason)."""
    prefix = sandbox_prefix(run.session_id)
    calls = [
        line
        for line in aps_lines
        if "decision_id" in line
        and line.get("route") == MCP_ROUTE
        and line.get("method") == "tools/call"
        and str(line.get("pod", "")).startswith(prefix)
    ]
    return sorted(calls, key=lambda line: str(line.get("ts", "")))


DECISION_FIELDS = ("ts", "decision_id", "reason", "tool", "agent_id", "session_id", "principal")


def status_from_summary(summary: str) -> int | None:
    m = STATUS_JSON.search(summary) or STATUS_WORD.search(summary)
    return int(m.group(1)) if m else None


# --- evaluation (pure, so the pass criteria are unit-testable) --------------------------------


def evaluate_t4(
    run: SessionRun,
    aps_lines: list[dict],
    tenant: str,
    claims: dict | None = None,
    *,
    agent: str = "conformance",
    issuer: str = "",
    read_error: str | None = None,
) -> dict:
    # Any generation of this session's sandbox: a replaced sandbox (spec 10.5) asks again.
    prefix = sandbox_prefix(run.session_id)
    issued = [
        line
        for line in aps_lines
        if line.get("msg") == "token issued"
        and str(line.get("pod", "")).startswith(prefix)
        and line.get("namespace") == tenant
    ]
    probes = run.tool_boundaries("probe_files")
    data = claims or {}
    audience = data.get("aud")
    iat, exp = data.get("iat"), data.get("exp")
    jti = data.get("jti")
    checks = {
        "session_completed": run.outcome == "COMPLETED",
        "probe_files_ran": bool(probes),
        "token_issued_for_pod": len(issued) >= 1,
        "one_token_per_sandbox": len(issued) == 1,
        "claims_from_snapshot": claims is not None and read_error is None,
        "audiences": isinstance(audience, list)
        and all(isinstance(a, str) for a in audience)
        and {"agent-gateway", "agent-policy-service"}.issubset(audience),
        "client_type": data.get("client_type") == "agent",
        "agent_id": data.get("agent_id") == f"{tenant}/{agent}",
        "issuer": bool(issuer) and data.get("iss") == issuer,
        "lifetime_300_seconds": type(iat) is int and type(exp) is int and exp - iat == 300,
        "jti_matches_issuance": isinstance(jti, str)
        and bool(jti)
        and any(
            d.get("jti") == jti
            and d.get("agent_id") == f"{tenant}/{agent}"
            and d.get("pod") == run.pod
            for d in issued
        ),
        "all_aps_logs_readable": not unreadable_pods(aps_lines),
    }
    required = [key for key in checks if key != "one_token_per_sandbox"]
    return {
        "result": "pass" if all(checks[k] for k in required) else "fail",
        "checks": checks,
        "evidence": {
            "session": asdict(run),
            "claims": scrub(allowlisted_claims(data)),
            "snapshot_read_error": read_error,
            "token_issued": [
                {
                    k: v
                    for k, v in line.items()
                    if k in ("time", "agent_id", "pod", "namespace", "jti")
                }
                for line in issued
            ],
            "aps_log_unreadable": unreadable_pods(aps_lines),
        },
    }


def evaluate_t5_sandbox(case: SandboxCase, run: SessionRun, aps_lines: list[dict]) -> dict:
    probes = run.tool_boundaries("probe_http")
    status = status_from_summary(run.final_summary)
    # The 400/401 refusal paths log no pod, so refusals in the case's window from any client
    # count; the status the probe reported is what ties the case to its own request.
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


def evaluate_t7(run: SessionRun, captures: list[dict], gateway_lines: list[dict]) -> dict:
    upstream = captures_of(run, captures)
    gateway = [
        line
        for line in gateway_lines
        if line.get("agent.session_id") == run.session_id
        and line.get("http.path") == GATEWAY_MODEL_CHAT_PATH
        and line.get("http.method") == "POST"
    ]
    model = [b for b in run.boundaries if b.get("kind") == "model"]

    def nonzero_usage(values: list) -> bool:
        try:
            counts = [int(v) for v in values]
        except (ValueError, TypeError):
            return False
        return bool(counts) and all(n >= 0 for n in counts) and sum(counts) > 0

    def no_agent_jwt(rec: dict) -> bool:
        bearer = rec.get("bearer") or {}
        return (
            bearer.get("error") == "not_a_jwt"
            and not bearer.get("jti")
            and not rec.get("jti")
            and not rec.get("bearer_jti")
        )

    checks = {
        "session_completed": run.outcome == "COMPLETED",
        "model_server_captured": bool(upstream),
        "chat_completion_at_model_server": bool(upstream)
        and all(r.get("method") == "POST" and r.get("path") == MODEL_CHAT_PATH for r in upstream),
        "model_server_status_200": bool(upstream) and all(r.get("status") == 200 for r in upstream),
        "backend_key_received": bool(upstream)
        and all(r.get("backend_key_match") is True for r in upstream),
        "no_agent_jwt_at_model_server": bool(upstream) and all(map(no_agent_jwt, upstream)),
        "gateway_accepted_agent_token": bool(gateway)
        and all(
            r.get("http.status") == "200"
            and r.get("jwt.sub")
            and r.get("agent.decision") == "allow"
            for r in gateway
        ),
        "boundary_usage_nonzero": nonzero_usage(
            [
                (b.get("usage") or {}).get(k)
                for b in model
                for k in ("input_tokens", "output_tokens")
            ]
        ),
        "gateway_usage_nonzero": nonzero_usage(
            [r.get(k) for r in gateway for k in ("llm.tokens_in", "llm.tokens_out")]
        ),
    }
    return {
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "evidence": {
            "session": asdict(run),
            "model_capture": [
                {
                    **{k: r.get(k) for k in ("ts", "method", "path", "status")},
                    "backend_key_match": r.get("backend_key_match") is True,
                    "no_agent_jwt": no_agent_jwt(r),
                }
                for r in upstream
            ],
            "gateway": [
                {
                    k: r.get(k)
                    for k in (
                        "http.method",
                        "http.path",
                        "http.status",
                        "agent.session_id",
                        "llm.tokens_in",
                        "llm.tokens_out",
                    )
                }
                for r in gateway
            ],
        },
    }


def evaluate_t8(
    run: SessionRun,
    aps_lines: list[dict],
    tenant: str,
    agent: str,
    captures: list[dict] | None = None,
) -> dict:
    calls = mcp_calls_of(run, aps_lines)
    allowed = [c for c in calls if c.get("tool") == MCP_ALLOWED_TOOL and c.get("reason") == "allow"]
    own = f"{tenant}/{agent}"
    identity_ok = bool(allowed) and all(
        c.get("agent_id") == own
        and c.get("session_id") == run.session_id
        and c.get("principal") != FORGED_HEADERS["x-agent-principal"]
        for c in allowed
    )
    forged = [v for v in FORGED_HEADERS.values() if v != "allow"]
    text = json.dumps([line for line in aps_lines if "decision_id" in line])
    checks = {
        "session_completed": run.outcome == "COMPLETED",
        "probe_mcp_ran": bool(run.tool_boundaries("probe_mcp")),
        "call_allowed": bool(allowed),
        "identity_is_the_sandboxes": identity_ok,
        "no_forged_value_in_decisions": not any(v in text for v in forged),
        "probe_reported_ok": "ok" in run.final_summary,
    }
    required = [
        "session_completed",
        "probe_mcp_ran",
        "call_allowed",
        "identity_is_the_sandboxes",
        "no_forged_value_in_decisions",
    ]
    evidence: dict[str, Any] = {
        "session": asdict(run),
        "decisions": [{k: c.get(k) for k in DECISION_FIELDS} for c in calls],
        "aps_log_unreadable": unreadable_pods(aps_lines),
    }
    if captures is None:
        evidence["upstream_capture"] = "not captured: no capture proxy log was read"
    else:
        # What the MCP server received (spec 15.2 T8: "capture at the MCP server").
        seen = captures_of(run, captures)
        headers = [r.get("x_agent", {}) for r in seen]
        values = [v for h in headers for v in h.values()]
        checks["upstream_captured"] = bool(seen)
        checks["upstream_received_aps_values"] = bool(seen) and all(
            h.get("x-agent-id") == own and h.get("x-agent-session-id") == run.session_id
            for h in headers
        )
        checks["upstream_no_forged_value"] = not any(v in values for v in forged)
        checks["upstream_no_decision_reason"] = not any(
            "x-agent-decision-reason" in h for h in headers
        )
        required += [
            "upstream_captured",
            "upstream_received_aps_values",
            "upstream_no_forged_value",
            "upstream_no_decision_reason",
        ]
        evidence["upstream_capture"] = [
            {k: r.get(k) for k in ("ts", "method", "path", "status", "x_agent")} for r in seen
        ]
    return {
        "result": "pass" if all(checks[k] for k in required) else "fail",
        "checks": checks,
        "evidence": evidence,
    }


def bearer_summary(record: dict) -> dict:
    b = record.get("bearer") or {}
    return {k: b.get(k) for k in ("sub", "act_sub", "aud", "azp", "jti", "error")}


def evaluate_t11(
    run: SessionRun,
    aps_lines: list[dict],
    captures: list[dict],
    gateway_lines: list[dict],
    tenant: str,
    agent: str,
) -> dict:
    calls = mcp_calls_of(run, aps_lines)
    allowed = [c for c in calls if c.get("tool") == MCP_ALLOWED_TOOL and c.get("reason") == "allow"]
    seen = captures_of(run, captures)
    bearers = [r.get("bearer") or {} for r in seen]
    subjects = inbound_subjects(run, gateway_lines)
    agent_sub = next(iter(subjects)) if len(subjects) == 1 else None
    checks = {
        "session_completed": run.outcome == "COMPLETED",
        "probe_mcp_ran": bool(run.tool_boundaries("probe_mcp")),
        "call_allowed": bool(allowed),
        "agent_mode": all(c.get("delegation") == "agent" for c in calls) and bool(calls),
        "upstream_captured": bool(seen) and all(b.get("sub") for b in bearers),
        "inbound_subject_known": agent_sub is not None,
        "sub_is_the_agent": bool(bearers)
        and agent_sub is not None
        and all(b.get("sub") == agent_sub for b in bearers),
        "no_act": bool(bearers) and all(not b.get("act_sub") for b in bearers),
        "aud_is_the_backend": bool(bearers)
        and all(
            any(str(a).endswith(f"/{MCP_BACKEND}/mcp") for a in (b.get("aud") or []))
            for b in bearers
        ),
        "upstream_status_ok": bool(seen) and all(int(r.get("status", 0)) < 400 for r in seen),
    }
    required = (
        "session_completed",
        "probe_mcp_ran",
        "call_allowed",
        "agent_mode",
        "upstream_captured",
        "inbound_subject_known",
        "sub_is_the_agent",
        "no_act",
    )
    return {
        "result": "pass" if all(checks[k] for k in required) else "fail",
        "checks": checks,
        "evidence": {
            "session": asdict(run),
            "decisions": [{k: c.get(k) for k in DECISION_FIELDS} for c in calls],
            "inbound_subjects": sorted(subjects),
            "upstream_capture": [
                {
                    **{k: r.get(k) for k in ("ts", "method", "path", "status")},
                    "bearer": bearer_summary(r),
                }
                for r in seen
            ],
            "aps_log_unreadable": unreadable_pods(aps_lines),
        },
    }


def evaluate_t12(run: SessionRun, aps_lines: list[dict], captures: list[dict], tenant: str) -> dict:
    prefix = sandbox_prefix(run.session_id)
    issued = [
        line
        for line in aps_lines
        if line.get("msg") == "token issued"
        and str(line.get("pod", "")).startswith(prefix)
        and line.get("namespace") == tenant
    ]
    agent_jtis = {str(line.get("jti")) for line in issued if line.get("jti")}
    exchanged = {
        str((line.get("exchange") or {}).get("upstream_jti"))
        for line in aps_lines
        if "decision_id" in line
        and str(line.get("pod", "")).startswith(prefix)
        and (line.get("exchange") or {}).get("upstream_jti")
    }
    seen = captures_of(run, captures)
    captured = [str((r.get("bearer") or {}).get("jti") or "") for r in seen]
    checks = {
        "session_completed": run.outcome == "COMPLETED",
        "agent_token_known": bool(agent_jtis),
        "upstream_captured": bool(seen) and all(captured),
        "agent_token_never_at_server": bool(captured)
        and not any(j in agent_jtis for j in captured),
        "every_upstream_token_was_exchanged": bool(captured)
        and all(j in exchanged for j in captured),
    }
    required = (
        "session_completed",
        "agent_token_known",
        "upstream_captured",
        "agent_token_never_at_server",
        "every_upstream_token_was_exchanged",
    )
    return {
        "result": "pass" if all(checks[k] for k in required) else "fail",
        "checks": checks,
        "evidence": {
            "session": asdict(run),
            "agent_token_jti": sorted(agent_jtis),
            "exchanged_jti": sorted(exchanged),
            "captured_jti": captured,
            "native_exchange_configuration": "not deployed (spec 8.11 optional); not covered",
            "aps_log_unreadable": unreadable_pods(aps_lines),
        },
    }


def evaluate_t19(
    run: SessionRun, aps_lines: list[dict], probe: dict | None, read_error: str | None = None
) -> dict:
    data = probe if isinstance(probe, dict) else {}
    requests = data.get("requests")
    requests = (
        requests
        if isinstance(requests, list) and all(isinstance(r, dict) for r in requests)
        else []
    )
    models = data.get("models")
    models = models if isinstance(models, list) and all(isinstance(r, dict) for r in models) else []
    elapsed, jti = data.get("elapsed_ms"), data.get("token_jti")
    mcp = mcp_calls_of(run, aps_lines)
    model_traces = {r.get("trace_id") for r in models if isinstance(r.get("trace_id"), str)}
    inference = [
        d
        for d in aps_lines
        if d.get("route") == "inference"
        and str(d.get("pod", "")).startswith(sandbox_prefix(run.session_id))
        and d.get("trace_id") in model_traces
    ]

    def correlate(requests, decisions, count):
        traces = [r.get("trace_id") for r in requests]
        valid = len(traces) == count and all(
            isinstance(t, str) and re.fullmatch(r"[0-9a-f]{32}", t) for t in traces
        )
        valid = bool(valid and len(set(traces)) == count)
        groups = [[d for d in decisions if d.get("trace_id") == t] for t in traces]
        return [d for group in groups for d in group], valid and len(decisions) == count and all(
            len(g) == 1 for g in groups
        )

    mcp_ordered, mcp_correlated = correlate(requests, mcp, 2 * T19_ROUNDS)
    model_ordered, model_correlated = correlate(models, inference, 2 * T19_MODEL_ROUNDS)
    decisions = mcp_ordered + model_ordered
    ids = [d.get("decision_id") for d in decisions]
    expected_mcp = [(MCP_ALLOWED_TOOL, "allow"), (MCP_FORBIDDEN_TOOL, DENY_ROUTE)] * T19_ROUNDS
    expected_models = [("llm-main", "allow"), ("llm-fast", DENY_ROUTE)] * T19_MODEL_ROUNDS
    valid_time = type(elapsed) in (int, float) and math.isfinite(elapsed) and 0 <= elapsed <= 10_000
    checks = {
        "session_completed": run.outcome == "COMPLETED",
        "one_probe_mcp_boundary": len(run.tool_boundaries("probe_mcp")) == 1,
        "probe_read_from_snapshot": probe is not None and read_error is None,
        "all_aps_logs_readable": not unreadable_pods(aps_lines),
        "mcp_within_10_seconds": valid_time,
        "mcp_http_statuses": [r.get("status") for r in requests] == [200, 403] * T19_ROUNDS,
        "mcp_results": data.get("calls") == ["ok", "deny"] * T19_ROUNDS,
        "one_mcp_decision_per_request": mcp_correlated,
        "exact_mcp_alternation": [(d.get("tool"), d.get("reason")) for d in mcp_ordered]
        == expected_mcp,
        "model_http_statuses": [(r.get("backend"), r.get("status")) for r in models]
        == [("llm-main", 200), ("llm-fast", 403)] * T19_MODEL_ROUNDS,
        "one_model_decision_per_request": model_correlated,
        "exact_model_alternation": [(d.get("backend"), d.get("reason")) for d in model_ordered]
        == expected_models,
        "distinct_decision_ids": len(ids) == 2 * (T19_ROUNDS + T19_MODEL_ROUNDS)
        and all(isinstance(i, str) and i for i in ids)
        and len(set(ids)) == len(ids),
        "one_token": isinstance(jti, str)
        and bool(jti)
        and bool(decisions)
        and all(d.get("token_jti") == jti for d in decisions),
    }
    fields = (*DECISION_FIELDS, "backend", "trace_id", "token_jti")
    return {
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "evidence": {
            "session": asdict(run),
            "rounds": T19_ROUNDS,
            "model_rounds": T19_MODEL_ROUNDS,
            "snapshot_read_error": read_error,
            "elapsed_ms": elapsed
            if type(elapsed) in (int, float) and math.isfinite(elapsed)
            else None,
            "token_jti": jti,
            "mcp_requests": [
                {k: r.get(k) for k in ("tool", "status", "trace_id")} for r in requests
            ],
            "model_requests": [
                {k: r.get(k) for k in ("backend", "status", "trace_id")} for r in models
            ],
            "mcp_decisions": [{k: d.get(k) for k in fields} for d in mcp_ordered],
            "model_decisions": [{k: d.get(k) for k in fields} for d in model_ordered],
            "aps_log_unreadable": unreadable_pods(aps_lines),
        },
    }


def mcp_decisions_of(run: SessionRun, aps_lines: list[dict]) -> list[dict]:
    """Every APS decision on the MCP route for this session's sandbox, in time order."""
    prefix = sandbox_prefix(run.session_id)
    found = [
        line
        for line in aps_lines
        if "decision_id" in line
        and line.get("route") == MCP_ROUTE
        and str(line.get("pod", "")).startswith(prefix)
    ]
    return sorted(found, key=lambda line: str(line.get("ts", "")))


def evaluate_t26(
    stash: SessionRun,
    same: SessionRun,
    other: SessionRun,
    token_jti: str | None,
    read_error: str | None,
    aps_lines: list[dict],
    tenant: str,
) -> dict:
    prefix = sandbox_prefix(stash.session_id)
    issued = [
        str(line.get("jti") or "")
        for line in aps_lines
        if line.get("msg") == "token issued"
        and str(line.get("pod", "")).startswith(prefix)
        and line.get("namespace") == tenant
    ]
    same_reasons = [d.get("reason") for d in mcp_decisions_of(same, aps_lines)]
    other_reasons = [d.get("reason") for d in mcp_decisions_of(other, aps_lines)]
    checks = {
        "stash_session_completed": stash.outcome == "COMPLETED",
        "stash_probe_ran": bool(stash.tool_boundaries("probe_token_stash")),
        "snapshot_recorded": bool(stash.snapshot),
        "token_read_from_snapshot": read_error is None and token_jti is not None,
        "token_is_the_stash_pods": token_jti is not None and token_jti in issued,
        "same_agent_session_completed": same.outcome == "COMPLETED",
        "same_agent_status_403": status_from_summary(same.final_summary) == 403,
        "same_agent_denied_bound_elsewhere": DENY_BOUND in same_reasons,
        "same_agent_never_allowed": "allow" not in same_reasons,
        "other_agent_session_completed": other.outcome == "COMPLETED",
        "other_agent_status_403": status_from_summary(other.final_summary) == 403,
        "other_agent_denied_mismatch": DENY_MISMATCH in other_reasons,
        "other_agent_never_allowed": "allow" not in other_reasons,
    }
    required = (
        "stash_session_completed",
        "stash_probe_ran",
        "token_read_from_snapshot",
        "token_is_the_stash_pods",
        "same_agent_session_completed",
        "same_agent_denied_bound_elsewhere",
        "same_agent_never_allowed",
        "other_agent_session_completed",
        "other_agent_denied_mismatch",
        "other_agent_never_allowed",
    )
    return {
        "result": "pass" if all(checks[k] for k in required) else "fail",
        "checks": checks,
        "evidence": {
            "stash": {**asdict(stash), "snapshot_read_error": read_error},
            "token_jti": token_jti,
            "issued_jti": issued,
            "same_agent": {
                "session": asdict(same),
                "decisions": [
                    {k: d.get(k) for k in DECISION_FIELDS}
                    for d in mcp_decisions_of(same, aps_lines)
                ],
            },
            "other_agent": {
                "session": asdict(other),
                "decisions": [
                    {k: d.get(k) for k in DECISION_FIELDS}
                    for d in mcp_decisions_of(other, aps_lines)
                ],
            },
            "aps_log_unreadable": unreadable_pods(aps_lines),
        },
    }


def evaluate_t5_direct(case: DirectCase, status: int | None, body: str, error: str | None) -> dict:
    checks = {"status_expected": status == case.expect_status, "no_token_in_reply": True}
    if "access_token" in body:
        checks["no_token_in_reply"] = False
    try:
        reply = json.loads(body)
    except ValueError:
        reply = None
    oauth_error = reply.get("error") if isinstance(reply, dict) else None
    if case.name in ("api_audience", "human_token"):
        checks["invalid_client"] = (
            isinstance(reply, dict) and reply.get("error") == "invalid_client"
        )
    return {
        "case": case.name,
        "expected_status": case.expect_status,
        "observed_status": status,
        "result": "pass" if all(checks.values()) and error is None else "fail",
        "checks": checks,
        "evidence": {
            "oauth_error": oauth_error
            if oauth_error in ("invalid_client", "forbidden", "unsupported_grant_type")
            else None,
            "error": error,
        },
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a backend key must never follow an engine redirect


def model_status(url: str, key: str | None) -> int:
    """Read status only. Bodies and exception messages may contain the backend key."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
        raise ValueError("T6_MODEL_URL must name a direct HTTP model endpoint")
    headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as response:
        with response:
            return response.code


def read_backend_key(path: str) -> str:
    with Path(path).open("rb") as source:
        raw = source.read(16385)
    key = raw.decode("ascii").strip()
    if len(raw) > 16384 or not key or any(not 33 <= ord(c) <= 126 for c in key):
        raise ValueError("BACKEND_KEY_FILE must contain one nonempty raw key")
    return key


class Harness:
    def __init__(
        self,
        client: Client,
        tenant: str,
        agent: str,
        release: str,
        aps_logs: Callable[[float], Awaitable[str]],
        capture_logs: Callable[[float], Awaitable[str]] | None = None,
        gateway_logs: Callable[[float], Awaitable[str]] | None = None,
        aps_url: str = DEFAULT_APS_URL,
        issuer_token_path: str = DEFAULT_ISSUER_TOKEN_PATH,
        api_token_path: str = DEFAULT_API_TOKEN_PATH,
        session_timeout: float = SESSION_TIMEOUT,
        post: Callable[[str, dict[str, str]], tuple[int, str]] = post_token_form,
        artifact_dir: str = DEFAULT_ARTIFACT_DIR,
        agent_b: str | None = None,
        release_b: str | None = None,
        model_capture_logs: Callable[[float], Awaitable[str]] | None = None,
        model_url: str = "",
        backend_key_file: str = "",
        admission=None,
        issuer: str = "",
        api_subject: str = DEFAULT_API_SUBJECT,
        human_token_path: str = "",
        human_subject: str = "",
        human_username: str = "",
        human_client_id: str = "agent-cli",
        human_jwks_url: str = "",
        protocol=None,
        revocation=None,
        budget=None,
        fail_closed=None,
        checkpoint=None,
        lifecycle=None,
    ) -> None:
        self.client = client
        self.tenant, self.agent, self.release = tenant, agent, release
        self.aps_logs = aps_logs  # since-seconds -> raw log text of every APS pod
        self.capture_logs = capture_logs  # the capture sidecar on the kubernetes MCP server
        self.gateway_logs = gateway_logs  # the agents Gateway access log
        self.aps_url = aps_url
        self.issuer_token_path = issuer_token_path
        self.api_token_path = api_token_path
        self.session_timeout = session_timeout
        self.post = post
        self.artifact_dir = artifact_dir  # the runner's artifact store, mounted read-only
        self.agent_b, self.release_b = agent_b, release_b  # the second agent T26 needs
        self.model_capture_logs = model_capture_logs
        self.model_url, self.backend_key_file = model_url, backend_key_file
        self.admission = admission
        self.issuer = issuer
        self.api_subject = api_subject
        self.human_token_path = human_token_path
        self.human_subject, self.human_username = human_subject, human_username
        self.human_client_id = human_client_id
        self.human_jwks_url = (
            human_jwks_url or issuer.rstrip("/") + "/protocol/openid-connect/certs"
        )
        self.protocol = protocol
        self.revocation = revocation
        self.budget = budget
        self.fail_closed = fail_closed
        self.checkpoint = checkpoint
        self.lifecycle = lifecycle

    async def run_session(
        self,
        case: SandboxCase,
        agent: str | None = None,
        release: str | None = None,
        seed: str | None = None,
    ) -> SessionRun:
        session_id = str(uuid.uuid4())
        inp = SessionInput(
            self.tenant,
            agent or self.agent,
            release or self.release,
            session_id,
            None,
            case.task(),
            seed=seed,
        )
        run = SessionRun(session_id, pod_name(session_id, 1), "SUBMITTED", started=utcnow())
        handle = await submit(self.client, inp)
        try:
            result: SessionResult = await asyncio.wait_for(handle.result(), self.session_timeout)
            run.outcome = result.outcome
            run.generation = max(result.generation, 1)
            run.pod = pod_name(session_id, run.generation)
            run.snapshot = result.snapshot
        except WorkflowFailureError as e:
            run.outcome = f"FAILED:{failure_type(e.cause)}"
        except TimeoutError:
            # The workflow would otherwise run on beside the next case, up to its own deadline.
            run.outcome = "HARNESS_TIMEOUT"
            await handle.cancel()
        run.finished = utcnow()
        events = [ev async for ev in handle.fetch_history_events()]
        try:
            boundaries = await boundaries_from_events(events, self.client.data_converter)
        except Exception as e:  # noqa: BLE001 - a wrong key raises InvalidTag, not ValueError
            run.outcome = f"{run.outcome}:undecodable_history:{type(e).__name__}"
            boundaries = []
        run.boundaries = [asdict(b) for b in boundaries]
        try:
            run.failures = await failures_from_events(events, self.client.data_converter)
        except Exception as e:  # noqa: BLE001 - diagnostics must not fail the test
            run.failures = [
                {"activity": "?", "type": type(e).__name__, "message": "", "non_retryable": False}
            ]
        for b in reversed(boundaries):
            if b.kind == "model" and b.summary:
                run.final_summary = b.summary
                break
        return run

    async def aps_lines_since(self, started: float) -> list[dict]:
        return parse_aps_log(await self.aps_logs(max(1.0, time.time() - started + 5)))

    async def captures_since(self, started: float) -> list[dict] | None:
        if self.capture_logs is None:
            return None
        return parse_capture_log(await self.capture_logs(max(1.0, time.time() - started + 5)))

    async def gateway_since(self, started: float) -> list[dict]:
        if self.gateway_logs is None:
            return []
        return parse_gateway_log(await self.gateway_logs(max(1.0, time.time() - started + 5)))

    async def t4(self) -> dict:
        started = time.time()
        case = t4_case()
        run = await self.run_session(case)
        claims, read_error = None, None
        try:
            claims = await asyncio.to_thread(read_t4_claims, self.artifact_dir, run.snapshot)
        except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
            read_error = type(exc).__name__
        lines = []
        for attempt in range(LOG_READ_ATTEMPTS):
            lines = await self.aps_lines_since(started)
            # Another session's issuance must not suppress a retry for this one.
            matching = claims is not None and any(
                line.get("msg") == "token issued"
                and line.get("namespace") == self.tenant
                and line.get("pod") == run.pod
                and line.get("jti") == claims.get("jti")
                for line in lines
            )
            if matching and not unreadable_pods(lines):
                break
            if attempt + 1 < LOG_READ_ATTEMPTS:
                await asyncio.sleep(1)
        return {
            "id": "T4",
            "name": "sandbox token issuance",
            **evaluate_t4(
                run,
                lines,
                self.tenant,
                claims,
                agent=self.agent,
                issuer=self.issuer,
                read_error=read_error,
            ),
        }

    async def t5(self) -> dict:
        cases = []
        # Use short-lived mounted credentials before spending minutes on sandbox sessions.
        cid = client_id(self.tenant, self.agent)
        for direct in t5_direct_cases(
            self.issuer_token_path, self.api_token_path, self.human_token_path
        ):
            proof = None
            try:
                assertion = read_assertion(direct.assertion_path)
                if direct.name == "api_audience":
                    authenticated_as = await asyncio.to_thread(api_assertion_identity, assertion)
                    proof = verify_api_fixture(
                        assertion, authenticated_as, self.api_subject, self.issuer
                    )
                elif direct.name == "human_token":
                    jwks = await asyncio.to_thread(load_jwks, self.human_jwks_url)
                    proof = verify_human_fixture(
                        assertion,
                        jwks,
                        self.issuer,
                        self.human_subject,
                        self.human_username,
                        self.human_client_id,
                    )
            except Exception as e:  # noqa: BLE001 - unusable fixtures are evidence, never token text
                cases.append(
                    {
                        "case": direct.name,
                        "expected_status": direct.expect_status,
                        "observed_status": None,
                        "result": "error",
                        "checks": {"fixture_verified": False},
                        "evidence": {"fixture_error": type(e).__name__},
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
            result = evaluate_t5_direct(direct, status, body, error)
            if proof is not None:
                result["checks"]["fixture_verified"] = True
                result["evidence"]["fixture_claims"] = scrub(proof)
            cases.append(result)
        for case in t5_sandbox_cases(self.tenant, self.agent):
            started = time.time()
            run = await self.run_session(case)
            lines = await self.aps_lines_since(started)
            cases.append(evaluate_t5_sandbox(case, run, lines))
        return {
            "id": "T5",
            "name": "token route refusals",
            "result": summarize(cases),
            "cases": cases,
        }

    async def t6(self) -> dict:
        key = read_backend_key(self.backend_key_file)
        try:
            unauthenticated = await asyncio.to_thread(model_status, self.model_url, None)
            authenticated = await asyncio.to_thread(model_status, self.model_url, key)
        finally:
            del key
        checks = {
            "unauthenticated_401": unauthenticated == 401,
            "authenticated_200": authenticated == 200,
        }
        return {
            "id": "T6",
            "name": "model engine requires backend credential",
            "result": "pass" if all(checks.values()) else "fail",
            "checks": checks,
            "evidence": {
                "unauthenticated_status": unauthenticated,
                "authenticated_status": authenticated,
            },
        }

    async def t7(self) -> dict:
        started = time.time()
        run = await self.run_session(t4_case())
        captures = []
        if self.model_capture_logs is not None:
            captures = parse_capture_log(
                await self.model_capture_logs(max(1.0, time.time() - started + 5))
            )
        return {
            "id": "T7",
            "name": "model request uses backend key",
            **evaluate_t7(run, captures, await self.gateway_since(started)),
        }

    async def t8(self) -> dict:
        started = time.time()
        run = await self.run_session(t8_case(self.tenant))
        lines = await self.aps_lines_since(started)
        captures = await self.captures_since(started)
        return {
            "id": "T8",
            "name": "forged agent headers",
            **evaluate_t8(run, lines, self.tenant, self.agent, captures),
        }

    async def t11(self) -> dict:
        started = time.time()
        run = await self.run_session(t11_case(self.tenant))
        lines = await self.aps_lines_since(started)
        captures = await self.captures_since(started) or []
        gateway = await self.gateway_since(started)
        return {
            "id": "T11",
            "name": "agent-mode upstream token",
            **evaluate_t11(run, lines, captures, gateway, self.tenant, self.agent),
        }

    async def t12(self) -> dict:
        started = time.time()
        run = await self.run_session(t11_case(self.tenant))
        lines = await self.aps_lines_since(started)
        captures = await self.captures_since(started) or []
        return {
            "id": "T12",
            "name": "agent token never reaches the MCP server",
            **evaluate_t12(run, lines, captures, self.tenant),
        }

    async def t26(self) -> dict:
        if not self.agent_b or not self.release_b:
            return {
                "id": "T26",
                "name": "token binding",
                "result": "error",
                "error": "T26 needs --agent-b and --release-b (a second approved agent)",
            }
        started = time.time()
        stash = await self.run_session(t26_stash_case())
        try:
            # Read only to compare by jti (15.4); the token itself goes nowhere.
            token = read_stashed_token(self.artifact_dir, stash.snapshot or "")
        except (OSError, ValueError, KeyError) as e:
            token, read_error = "", f"{type(e).__name__}: {e}"
        else:
            read_error = None
        same = await self.run_session(
            t26_present_case("same_agent_other_session"), seed=stash.snapshot
        )
        other = await self.run_session(
            t26_present_case("other_agent"), self.agent_b, self.release_b, seed=stash.snapshot
        )
        jti = jwt_jti(token)
        del token
        lines = await self.aps_lines_since(started)
        return {
            "id": "T26",
            "name": "token binding",
            **evaluate_t26(stash, same, other, jti, read_error, lines, self.tenant),
        }

    async def t19(self) -> dict:
        started = time.time()
        run = await self.run_session(t19_case(self.tenant))
        lines = await self.aps_lines_since(started)
        try:
            probe = await asyncio.to_thread(
                read_t19_probe_result, self.artifact_dir, run.snapshot or ""
            )
            read_error = None
        except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
            probe, read_error = None, type(exc).__name__
        return {
            "id": "T19",
            "name": "no decision caching",
            **evaluate_t19(run, lines, probe, read_error),
        }

    async def run(self, tests: list[str]) -> dict:
        started = utcnow()
        results = []
        for t in tests:
            try:
                target = (
                    self.protocol
                    if t in PROTOCOL_TESTS
                    else self.admission
                    if t in ADMISSION_TESTS
                    else self
                )
                if t in BUDGET_TESTS:
                    target = self.budget
                if t in REVOCATION_TESTS:
                    target = self.revocation
                if t == "T23":
                    target = self.checkpoint
                if t == "T17":
                    target = self.fail_closed
                if t == "T24":
                    target = self.lifecycle
                if target is None:
                    raise ValueError("selected test requires its Kubernetes adapter")
                results.append(await getattr(target, t.lower())())
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
    return pod_log_reader(core, APS_NAMESPACE, APS_LABEL, APS_CONTAINER)


def pod_log_reader(
    core, namespace: str, label: str, container: str | None
) -> Callable[[float], Awaitable[str]]:
    """Read the logs of every pod matching `label` in `namespace` from the last `since` seconds,
    one pod after another; a pod whose log cannot be read after LOG_READ_ATTEMPTS is recorded
    as a `log unavailable` line, never silently skipped."""

    async def read(since: float) -> str:
        # Kubernetes' client exposes a relative since_seconds option. Preserve
        # the original cutoff across slow discovery, earlier replicas and retries.
        cutoff = time.monotonic() - since

        def blocking() -> str:
            pods = core.list_namespaced_pod(
                namespace, label_selector=label, _request_timeout=(5, 15)
            )
            chunks = []
            for pod in pods.items:
                last: Exception | None = None
                for attempt in range(LOG_READ_ATTEMPTS):
                    try:
                        kwargs = {"container": container} if container else {}
                        chunks.append(
                            core.read_namespaced_pod_log(
                                pod.metadata.name,
                                namespace,
                                since_seconds=max(1, math.ceil(time.monotonic() - cutoff) + 1),
                                _request_timeout=(5, 15),
                                **kwargs,
                            )
                        )
                        last = None
                        break
                    except Exception as e:  # noqa: BLE001 - a transient API error or a restart
                        last = e
                        time.sleep(1)
                if last is not None:
                    chunks.append(
                        json.dumps(
                            {
                                "msg": LOG_UNAVAILABLE,
                                "pod": pod.metadata.name,
                                "error": type(last).__name__,
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
    temporal = await connect(args.tenant)
    admission = None
    protocol = None
    if any(t in ADMISSION_TESTS for t in args.tests):
        from runner.conformance_admission import (
            AdmissionHarness,
            AdmissionSessions,
            KubernetesAdmission,
        )

        ops = KubernetesAdmission(core, k8s.CustomObjectsApi(), args.tenant)
        admission = AdmissionHarness(
            ops,
            AdmissionSessions(temporal, ops, timeout=args.session_timeout),
            args.admission_fixture_agent or args.agent,
            args.admission_fixture_release or args.release,
            unsigned_release=args.admission_unsigned_release,
            allow_mutations=args.allow_admission_mutations,
        )
    if "T25" in args.tests:
        from runner.conformance_protocol import (
            KubernetesProtocol,
            ProtocolHarness,
            ProtocolSessions,
        )

        ops = KubernetesProtocol(core, k8s.CustomObjectsApi(), args.tenant)
        protocol = ProtocolHarness(
            ops,
            ProtocolSessions(
                temporal,
                ops,
                args.protocol_fixture_agent,
                args.protocol_fixture_release,
                timeout=args.session_timeout,
            ),
            pod_log_reader(
                core,
                "agent-platform",
                "app.kubernetes.io/name=session-runner," + "agentplatform.io/tenant=" + args.tenant,
                "runner",
            ),
            args.protocol_fixture_agent,
            args.protocol_fixture_release,
        )
    budget = None
    if any(t in BUDGET_TESTS for t in args.tests):
        from runner.conformance_admission import KubernetesAdmission
        from runner.conformance_budget_runtime import (
            BudgetHarness,
            BudgetObserver,
            KubernetesGatewayWatch,
        )

        budget = BudgetHarness(
            temporal,
            KubernetesAdmission(core, k8s.CustomObjectsApi(), args.tenant),
            BudgetObserver(
                os.environ.get("APS_URL", DEFAULT_APS_URL),
                os.environ.get("APS_TOKEN_PATH", DEFAULT_BUDGET_TOKEN_PATH),
            ),
            aps_log_reader(core),
            pod_log_reader(
                core,
                os.environ.get("BUDGET_BACKEND_NAMESPACE", "agent-platform"),
                os.environ.get("BUDGET_BACKEND_LABEL", "app.kubernetes.io/name=budget-backend"),
                "budget-backend",
            ),
            KubernetesGatewayWatch(core),
            args.budget_day_release,
            args.budget_session_release,
            os.environ.get("ARTIFACT_DIR", DEFAULT_ARTIFACT_DIR),
        )
    harness = Harness(
        temporal,
        args.tenant,
        args.agent,
        args.release,
        aps_log_reader(core),
        capture_logs=pod_log_reader(core, CAPTURE_NAMESPACE, CAPTURE_LABEL, CAPTURE_CONTAINER),
        gateway_logs=pod_log_reader(core, GATEWAY_NAMESPACE, GATEWAY_LABEL, GATEWAY_CONTAINER),
        aps_url=os.environ.get("APS_URL", DEFAULT_APS_URL),
        issuer_token_path=os.environ.get("ISSUER_TOKEN_PATH", DEFAULT_ISSUER_TOKEN_PATH),
        api_token_path=os.environ.get("API_TOKEN_PATH", DEFAULT_API_TOKEN_PATH),
        session_timeout=args.session_timeout,
        artifact_dir=os.environ.get("ARTIFACT_DIR", DEFAULT_ARTIFACT_DIR),
        agent_b=args.agent_b,
        release_b=args.release_b,
        model_capture_logs=pod_log_reader(
            core, MODEL_CAPTURE_NAMESPACE, MODEL_CAPTURE_LABEL, MODEL_CAPTURE_CONTAINER
        ),
        model_url=os.environ.get("T6_MODEL_URL", ""),
        backend_key_file=os.environ.get("BACKEND_KEY_FILE", ""),
        admission=admission,
        issuer=os.environ.get("ISSUER_URL", ""),
        api_subject=os.environ.get("T5_API_SUBJECT", DEFAULT_API_SUBJECT),
        human_token_path=os.environ.get("T5_HUMAN_TOKEN_FILE", ""),
        human_subject=os.environ.get("T5_HUMAN_SUBJECT", ""),
        human_username=os.environ.get("T5_HUMAN_USERNAME", ""),
        human_client_id=os.environ.get("T5_HUMAN_CLIENT_ID", "agent-cli"),
        human_jwks_url=os.environ.get("T5_HUMAN_JWKS_URL", ""),
        protocol=protocol,
        budget=budget,
    )
    if any(t in REVOCATION_TESTS for t in args.tests):
        from runner.conformance_revocation import RevocationHarness, RevocationOps

        ops = RevocationOps(core, k8s.CustomObjectsApi(), args.tenant, poll=0.1)
        harness.revocation = RevocationHarness(
            ops,
            temporal,
            harness,
            args.revocation_fixture_release,
            allow_mutations=args.allow_revocation_mutations,
        )
    if "T17" in args.tests:
        from runner.conformance_fail_closed_runtime import FailClosedHarness, OutageOps

        harness.fail_closed = FailClosedHarness(
            temporal,
            OutageOps(core, k8s.CustomObjectsApi(), k8s.AppsV1Api()),
            harness.gateway_logs,
            args.fail_closed_fixture_release,
            args.aps_outage_marker,
            args.aps_outage_duration,
            harness.artifact_dir,
            timeout=args.session_timeout,
        )
    if "T23" in args.tests:
        from runner.conformance_checkpoint_runtime import CheckpointHarness, KubernetesCheckpoint

        harness.checkpoint = CheckpointHarness(
            KubernetesCheckpoint(core, k8s.CustomObjectsApi(), args.tenant),
            temporal,
            harness,
            args.checkpoint_fixture_release,
            harness.artifact_dir,
            allow_mutations=args.allow_checkpoint_faults,
            timeout=args.session_timeout,
        )
    if "T24" in args.tests:
        from runner.conformance_lifecycle_control import Control
        from runner.conformance_lifecycle_runtime import (
            LifecycleHarness,
            LifecycleObserver,
            LifecycleOps,
        )

        control = Control(core)
        harness.lifecycle = LifecycleHarness(
            temporal,
            LifecycleOps(core, k8s.CustomObjectsApi(), k8s.AppsV1Api(), control),
            control,
            LifecycleObserver(),
            args.lifecycle_fixture_release,
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
    parser.add_argument("--agent-b", default=None, help="T26: a second agent of the tenant")
    parser.add_argument("--release-b", default=None, help="T26: that agent's approved release")
    parser.add_argument("--admission-fixture-agent", help="dedicated T9a/T9b mutation fixture")
    parser.add_argument("--admission-fixture-release", help="that fixture's signed Ready release")
    parser.add_argument("--admission-unsigned-release", help="approved, unsigned fixture release")
    parser.add_argument(
        "--fail-closed-fixture-release", help="T17 signed Ready conformance-b release"
    )
    parser.add_argument(
        "--aps-outage-marker", help="T17 operator receipt marker: 16 lowercase hex digits"
    )
    parser.add_argument("--aps-outage-duration", type=int, default=180)
    parser.add_argument(
        "--observe-aps-outage",
        action="store_true",
        help="T17: observe operator-controlled APS outage; never mutate",
    )
    parser.add_argument(
        "--lifecycle-fixture-release", help="T24 signed Ready conformance-lifecycle release"
    )
    parser.add_argument(
        "--observe-lifecycle-faults",
        action="store_true",
        help="T24: observe root-controlled faults on exact isolated fixture sessions",
    )
    parser.add_argument("--protocol-fixture-agent", help="dedicated signed T25 protocol fixture")
    parser.add_argument("--protocol-fixture-release", help="that fixture's approved Ready release")
    parser.add_argument(
        "--revocation-fixture-release",
        help="T15/T20: signed Ready conformance-b release with probe_revocation",
    )
    parser.add_argument(
        "--allow-revocation-mutations",
        action="store_true",
        help="temporarily disable/drift conformance-b and hold one sandbox deletion, then restore",
    )
    parser.add_argument(
        "--allow-admission-mutations",
        action="store_true",
        help="temporarily revoke approval/drift the explicit fixture, then restore",
    )
    parser.add_argument(
        "--budget-day-release", help="signed conformance-budget-day release for T13"
    )
    parser.add_argument(
        "--budget-session-release", help="signed conformance-budget-session release for T13b"
    )
    parser.add_argument(
        "--checkpoint-fixture-release", help="signed Ready conformance-checkpoint release for T23"
    )
    parser.add_argument(
        "--allow-checkpoint-faults",
        action="store_true",
        help="T23: authorize exact operator-bound checkpoint gates",
    )
    parser.add_argument("--report", type=Path, default=None, help="write the JSON report here too")
    parser.add_argument("--session-timeout", type=float, default=SESSION_TIMEOUT)
    args = parser.parse_args(argv)
    supported = (
        *TESTS,
        *ADMISSION_TESTS,
        *PROTOCOL_TESTS,
        *REVOCATION_TESTS,
        *BUDGET_TESTS,
        "T17",
        "T23",
        "T24",
    )
    known = {t.upper(): t for t in supported}
    args.tests = [
        known.get(t.strip().upper(), t.strip().upper()) for t in args.tests.split(",") if t.strip()
    ]
    unknown = [t for t in args.tests if t not in supported]
    if unknown:
        parser.error(f"unknown tests {unknown}; known: {', '.join(supported)}")
    if "T23" in args.tests and not (
        args.tests == ["T23"]
        and args.tenant == "tenant-test"
        and args.checkpoint_fixture_release
        and args.allow_checkpoint_faults
        and args.session_timeout >= 300
    ):
        parser.error(
            "T23 requires exclusive selection, tenant-test, signed fixture, --allow-checkpoint-faults and --session-timeout >=300"
        )
    if "T17" in args.tests and not (
        args.tests == ["T17"]
        and args.tenant == "tenant-test"
        and args.observe_aps_outage
        and args.fail_closed_fixture_release
        and re.fullmatch(r"[0-9a-f]{16}", args.aps_outage_marker or "")
        and 60 <= args.aps_outage_duration <= 240
    ):
        parser.error(
            "T17 requires exclusive selection, tenant-test, explicit signed fixture, 16-hex marker and --observe-aps-outage (duration 60..240)"
        )
    if "T24" in args.tests and not (
        args.tests == ["T24"]
        and args.tenant == "tenant-test"
        and args.lifecycle_fixture_release
        and args.observe_lifecycle_faults
    ):
        parser.error(
            "T24 requires exclusive selection, tenant-test, explicit signed lifecycle fixture and --observe-lifecycle-faults"
        )
    if "T25" in args.tests and not (
        args.protocol_fixture_agent == "conformance-protocol"
        and args.protocol_fixture_release
        and args.session_timeout >= 1800
    ):
        parser.error(
            "T25 requires --protocol-fixture-agent conformance-protocol, explicit release, and --session-timeout >=1800"
        )
    if any(t in REVOCATION_TESTS for t in args.tests):
        if not args.allow_revocation_mutations or not args.revocation_fixture_release:
            parser.error(
                "T15/T20 require --revocation-fixture-release and --allow-revocation-mutations"
            )
        if args.agent == "conformance-b":
            parser.error("revocation mutations must not target the primary conformance agent")
    if bool(args.admission_fixture_agent) != bool(args.admission_fixture_release):
        parser.error("admission fixture agent and release must be supplied together")
    if any(t in ("T9a", "T9b") for t in args.tests):
        if not (
            args.allow_admission_mutations
            and args.admission_fixture_agent
            and args.admission_fixture_release
            and args.admission_unsigned_release
        ):
            parser.error(
                "T9a/T9b require explicit fixture agent/release, unsigned release and --allow-admission-mutations"
            )
        if args.admission_fixture_agent == args.agent:
            parser.error(
                "admission mutations must use a separate agent, never the primary conformance agent"
            )
    if "T13" in args.tests and not args.budget_day_release:
        parser.error("T13 requires --budget-day-release and external gateway pod replacement")
    if "T13b" in args.tests and not args.budget_session_release:
        parser.error("T13b requires --budget-session-release")
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
