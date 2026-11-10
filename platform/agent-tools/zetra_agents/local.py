"""Run one whole session of an agent on the developer's machine.

`zetra-agents run` drives the SDK's own boundary driver, one process per boundary, the
way the Session Runner does: the same length-prefixed request, state file, tool dispatch,
exec deadline and exit codes, against the `/app` layout an image has (read
only while the session runs, as it is there). Two things are replaced,
because a laptop has neither: the model route (the scenario's scripted turns, or with
`live` an OpenAI-compatible endpoint from the environment) and the MCP route (the
scenario's scripted replies, behind the grants in `deploy/agent.yaml`). That file is
first held to the Agent CRD's schema as `zetra_agents.schema` reads it, which is not in
every respect how the API server does (the differences are listed there); the grants, the
session's token budget and its boundary limit are then read from the policy bundle the
contracts compile the Agent to. The boundary limit is the Agent's
`spec.runtime.maxBoundaries`. A Session Runner built with zetra #76 reads it from the same
bundle and stops a session it starts at the smaller of it and what the submitter passes
(64 unless told otherwise); a runner built before that, and a session already running
when the runner is replaced, are held to the submitter's limit alone. A local run refuses
a request above the Agent's limit where the platform ends the session at it. The token
budget is where a local run is laxer: the step is
given the figure as the pod template gives it, but what holds a platform session to it is
the gateway's reservation before each model call, and that is not here. Only the SDK's
own advisory check is, and a scripted model reports no usage for it to count. An agent
whose Agent is outside the schema as read here, whose layout `zetra-agents check`
refuses, or whose Agent does not compile, is not run. A step counts only when it exited
as a step may and printed what the Session Runner would accept as its boundary's result
(`step_result`); a tool can print that itself, there as here.

So a local run shows what the agent does with a task: which tools it calls, what the
grants would refuse, what the report says. It is not a deployment check. Identity, the
gateway's policy and budgets, the sandbox VM, the network policy, the signed image and
the real MCP servers are exercised only by a session on the platform. Two more
differences can let a local run pass where the platform would not: the agent imports
from the whole workspace's environment rather than the image's own packages (the CI image
check is what proves those), and the task is not screened for credentials as the Session
Runner screens it. The Agent's `ttlSeconds` is not modelled either, and a process a tool
leaves running is not ended with the run.

Nothing here contains the agent's code. A step is an ordinary process running as the
developer: the read-only layout and the clean environment make an accidental write or a
dumped environment behave as they would in the image, but code that sets out to can read
and write whatever the developer can, this run's own bookkeeping and the `live`
endpoint's key included. A local pass says nothing about code one does not trust.

A scenario is a YAML file, by convention `agents/<agent>/scenarios/<name>.yaml`:

    task: Why is the checkout pod restarting?      # text, sent as {"request": text},
                                                   # or a mapping sent as given
    mcp:                                           # what each granted MCP tool returns
      - tool: pods_list_in_namespace
        backend: kubernetes                        # optional: any backend otherwise
        result: {items: []}                        # text, or a document sent as JSON
      - tool: events_list
        error: upstream unavailable                # the call fails instead
    model:                                         # omit for a live model
      - call: list_pods
        arguments: {namespace: shop}
      - call: write_report
        arguments: {content: Nothing is restarting.}
      - say: The report is written.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml
from agent_platform.bundle import compile_bundle
from agent_platform.constants import DEFAULT_TRUST_DOMAIN

from zetra_agents import layout, local_step, schema, testing
from zetra_agents.layout import AgentDir

BOUNDARY_SECONDS = 290.0  # the Session Runner's exec deadline
MAX_TASK = 16 * 1024  # `just agent run` in Kubani refuses a larger task
MAX_REPORT = 16_000  # bytes of workspace/report.md the platform's report reader accepts
MAX_SNAPSHOT = 64 * 1024 * 1024  # the Session Runner's limit on the tar of workspace/ and state/
MAX_OUTPUT = 65536  # the Session Runner's limit on what a step prints
MAX_SUMMARY = 512
RESULT_FIELDS = {"boundary", "kind", "done", "tool", "usage", "summary"}
# The exit codes the Session Runner reads a result after; any other is a failed boundary.
OUTCOMES = {0: "running", 2: "completed", 3: "agent_error", 4: "budget"}
NOT_A_DEPLOYMENT_CHECK = (
    "local run: the model and MCP are stand-ins; identity, gateway policy, budgets, the "
    "sandbox and the signed image are exercised only on the platform"
)


class ScenarioError(ValueError):
    """The scenario, task or agent cannot be run locally as given."""


@dataclass(frozen=True)
class Scenario:
    task: Any = None
    mcp: tuple[dict, ...] = ()
    model: tuple[dict, ...] | None = None
    source: str = ""


@dataclass
class Result:
    agent: str
    outcome: str
    model: str
    directory: Path
    boundaries: list[dict] = field(default_factory=list)
    mcp: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    report: Path | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == "completed" and not self.problems

    @property
    def transcript(self) -> Path:
        return self.directory / "sandbox" / "state" / "state.json"

    def document(self) -> dict:
        usage = [b.get("usage") or {} for b in self.boundaries]
        return {
            "ok": self.ok,
            "agent": self.agent,
            "outcome": self.outcome,
            "detail": self.detail,
            "model": self.model,
            "boundaries": len(self.boundaries),
            "tools": [b["tool"] for b in self.boundaries if b.get("tool")],
            "usage": {
                "input_tokens": sum(u.get("input_tokens") or 0 for u in usage),
                "output_tokens": sum(u.get("output_tokens") or 0 for u in usage),
            },
            "mcp": {
                outcome: sum(1 for c in self.mcp if c["outcome"] == outcome)
                for outcome in ("ok", "error", "denied", "unscripted", "invalid")
            },
            "problems": self.problems,
            "report": str(self.report) if self.report else None,
            "transcript": str(self.transcript),
            "runDirectory": str(self.directory),
            "note": NOT_A_DEPLOYMENT_CHECK,
        }


def _keys(where: str, entry: Any, required: set[str], optional: set[str]) -> dict:
    if not isinstance(entry, dict):
        raise ScenarioError(f"{where} must be a mapping")
    unknown, missing = set(entry) - required - optional, required - set(entry)
    if unknown:
        raise ScenarioError(f"{where}: unknown {sorted(unknown)}")
    if missing:
        raise ScenarioError(f"{where}: missing {sorted(missing)}")
    return entry


def parse_scenario(document: Any, source: str = "") -> Scenario:
    where = source or "scenario"
    document = _keys(where, document, set(), {"task", "mcp", "model"})
    try:
        json.dumps(document, allow_nan=False)
    except (TypeError, ValueError):
        # YAML reads an unquoted 2026-11-10 as a date, which no task or reply can carry.
        raise ScenarioError(f"{where}: every value must be JSON; quote dates and times") from None
    for key in ("mcp", "model"):
        if not isinstance(document.get(key) or [], list):
            raise ScenarioError(f"{where}: {key} is a list")
    replies = []
    for i, entry in enumerate(document.get("mcp") or []):
        at = f"{where}: mcp[{i}]"
        entry = _keys(at, entry, {"tool"}, {"backend", "result", "error"})
        if ("result" in entry) == ("error" in entry):
            raise ScenarioError(f"{at} needs exactly one of result and error")
        if not all(isinstance(entry.get(k, ""), str) for k in ("tool", "backend", "error")):
            raise ScenarioError(f"{at}: tool, backend and error are text")
        replies.append(dict(entry))
    turns = None
    if document.get("model") is not None:
        turns = []
        for i, entry in enumerate(document["model"]):
            at = f"{where}: model[{i}]"
            if isinstance(entry, dict) and "say" in entry:
                entry = _keys(at, entry, {"say"}, set())
                if not isinstance(entry["say"], str):
                    raise ScenarioError(f"{at}: say is text")
            else:
                entry = _keys(at, entry, {"call"}, {"arguments"})
                if not isinstance(entry["call"], str) or not isinstance(
                    entry.get("arguments") or {}, dict
                ):
                    raise ScenarioError(f"{at}: call is a tool name and arguments a mapping")
            turns.append(dict(entry))
        if not turns:
            raise ScenarioError(f"{where}: model has no turns; omit it to use a live model")
    task = document.get("task")
    return Scenario(
        task={"request": task} if isinstance(task, str) else task,
        mcp=tuple(replies),
        model=None if turns is None else tuple(turns),
        source=source,
    )


def scenario_path(agent: AgentDir, name: str | Path) -> Path:
    """A file as given, else `agents/<agent>/scenarios/<name>.yaml`."""
    given = Path(name)
    if given.is_file():
        return given
    named = agent.root / "scenarios" / f"{name}.yaml"
    if named.is_file():
        return named
    raise ScenarioError(f"no scenario {name}: neither a file nor {named}")


def load_scenario(agent: AgentDir, name: str | Path) -> Scenario:
    path = scenario_path(agent, name)
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: not YAML: {exc}") from None
    except UnicodeDecodeError:
        raise ScenarioError(f"{path}: not UTF-8") from None
    return parse_scenario(document, str(path))


def task_document(task: Any) -> Any:
    """The task as it is sent, measured as `just agent run` in Kubani measures it."""
    if task is None:
        raise ScenarioError("no task: give --task, --task-json or a scenario with `task:`")
    try:
        encoded = json.dumps(task, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ScenarioError("the task must be JSON") from None
    if len(encoded.encode()) > MAX_TASK:
        raise ScenarioError("the task is larger than 16 KiB")
    return task


def state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "zetra" / "agent-runs"


def new_directory(agent: AgentDir, out: Path | None) -> Path:
    if out is None:
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        out = state_home() / agent.name / f"{stamp}-{secrets.token_hex(2)}"
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise ScenarioError(f"{out} is not a directory")
    if out.exists() and any(out.iterdir()):
        raise ScenarioError(f"{out} is not empty; a run directory is never reused")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    return out.resolve()


def compiled(agent: AgentDir) -> dict:
    """The policy bundle the contracts compile this Agent to. Its MCP grants and session
    token budget are what the gateway holds a session to on the platform; its boundary
    limit is the Agent's own, which a Session Runner built with zetra #76 reads from the
    release's bundle and holds a session to, along with the submitter's limit."""
    release = {"metadata": {"name": "local"}, "spec": {"agent": agent.name, "image": "local"}}
    try:
        return compile_bundle(
            agent.agent_cr(),
            release,
            mcp_resource_base="https://mcp.invalid",
            trust_domain=DEFAULT_TRUST_DOMAIN,
        )
    except (
        yaml.YAMLError,
        ArithmeticError,
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        why = exc if isinstance(exc, ValueError) else f"{type(exc).__name__} {exc}"
        raise ScenarioError(
            f"{agent.name}: deploy/agent.yaml does not compile to a release policy: {_clip(why)}"
        ) from None


def _writable(root: Path, on: bool) -> None:
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink():
            mode = path.stat().st_mode
            path.chmod(mode | 0o200 if on else mode & ~0o222)


def refusals(agent: AgentDir) -> list[str]:
    """Why this agent is not run: its Agent is outside the CRD, or `check` refuses it, so
    the grants and limits a local session is held to are ones the platform could have."""
    problems = layout.check_files(agent)
    if problems:
        return problems
    if layout.image_target(agent) != "runtime":
        return [f"{agent.name} is a conformance fixture; it runs only in its own image"]
    try:
        refused = schema.problems((agent.agent_cr() or {}).get("spec"))
        if refused:
            return [
                f"{agent.name}: deploy/agent.yaml is outside the Agent CRD: {p}" for p in refused
            ]
        problems = layout.check(agent)
        if not problems and not isinstance(agent.manifest().get("model"), dict):
            return [f"{agent.name} declares no model; there is no model loop to run"]
    except (yaml.YAMLError, AttributeError, KeyError, TypeError, ValueError) as exc:
        why = f"{type(exc).__name__} {exc}"
        return [f"{agent.name}: agent.yaml or deploy/agent.yaml is malformed: {_clip(why)}"]
    return problems


def _clip(text: str, limit: int = 160) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _last_message(directory: Path) -> dict:
    try:
        state = json.loads((directory / "sandbox/state/state.json").read_text(encoding="utf-8"))
        return state["messages"][-1]
    except (OSError, ValueError, KeyError, IndexError):
        return {}


def _detail(record: dict, last: dict) -> str:
    """What the boundary did, read from the transcript the step just wrote."""
    if record.get("kind") == "tool" and last.get("role") == "tool":
        return f"{record.get('tool')} -> {_clip(last.get('content', ''))}"
    calls = last.get("tool_calls") or []
    if last.get("role") == "assistant" and calls:
        fn = calls[0].get("function") or {}
        return f"calls {fn.get('name')}({_clip(fn.get('arguments', ''))})"
    return _clip(record.get("summary", ""))


def mcp_problems(agent: AgentDir, calls: list[dict]) -> list[str]:
    found = []
    denied = sorted({(c["backend"], c["tool"]) for c in calls if c["outcome"] == "denied"})
    grants = testing.mcp_grants(agent.agent_cr()) if denied else {}
    for backend, tool in denied:
        found.append(testing.ungranted(agent.name, backend, tool, entry=backend in grants))
    for backend, tool in sorted(
        {(c["backend"], c["tool"]) for c in calls if c["outcome"] == "unscripted"}
    ):
        found.append(
            f"{agent.name} called MCP {backend}/{tool} and the scenario has no reply for it; "
            f"add `- tool: {tool}` with a `result:` under `mcp:`"
        )
    return found


def step_result(stdout: bytes, boundary: int, rc: int) -> dict | None:
    """What a step printed, if the Session Runner would take it as this boundary's result
    (spec 10.4; zetra platform/runner `BoundaryOut.parse_strict` and `run_boundary`): at
    most 64 KiB, exactly the six fields with their types, this boundary, and `done` true
    exactly when the step exited 2."""

    def count(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    if len(stdout) > MAX_OUTPUT:
        return None
    try:
        doc = json.loads(stdout.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or set(doc) != RESULT_FIELDS:
        return None
    tool, usage, summary = doc["tool"], doc["usage"], doc["summary"]
    accepted = (
        count(doc["boundary"])
        and doc["boundary"] == boundary
        and doc["kind"] in ("model", "tool")
        and doc["done"] is (rc == 2)
        and (tool is None or (isinstance(tool, str) and 0 < len(tool) <= 128))
        and (
            usage is None
            or (
                isinstance(usage, dict)
                and set(usage) == {"input_tokens", "output_tokens"}
                and all(count(v) for v in usage.values())
            )
        )
        and isinstance(summary, str)
        and len(summary) <= MAX_SUMMARY
    )
    return doc if accepted else None


def tar_bytes(sandbox: Path) -> int:
    """The entries GNU tar writes for `tar -C /sandbox --exclude=state/token.json -cf -
    workspace state`, in bytes, before the end-of-archive blocks: a 512-byte header for
    every entry, each file padded to 512, and a further header and the padded name for a
    name or link target longer than 100 bytes. Entries are read in tar's order, so a file or
    symbolic link with several names is stored once and linked afterwards, as tar does it. A socket is
    left out, as is any `state/token.json` and what is under it (the exclude is not
    anchored). Something that vanishes while it is being measured is left out too."""
    linked: dict[tuple[int, int], bytes] = {}

    def padded(length: int) -> int:
        return -(-length // 512) * 512

    def long(name: bytes) -> int:
        return 512 + padded(len(name) + 1) if len(name) > 100 else 0

    def entry(path: Path, name: str) -> int:
        if name == "state/token.json" or name.endswith("/state/token.json"):
            return 0
        try:
            status = path.lstat()
            mode = status.st_mode
            if stat.S_ISSOCK(mode):
                return 0
            if stat.S_ISDIR(mode):
                with os.scandir(path) as listing:
                    children = [child.name for child in listing]
                size = 512 + long(os.fsencode(name + "/"))
                return size + sum(entry(path / child, f"{name}/{child}") for child in children)
            size = 512 + long(os.fsencode(name))
            if status.st_nlink > 1 and (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                first = linked.setdefault((status.st_dev, status.st_ino), os.fsencode(name))
                if first != os.fsencode(name):
                    return size + long(first)
            if stat.S_ISLNK(mode):
                return size + long(os.fsencode(os.readlink(path)))
        except OSError:
            return 0
        return size + (padded(status.st_size) if stat.S_ISREG(mode) else 0)

    return sum(entry(sandbox / top, top) for top in ("workspace", "state"))


def snapshot_size(sandbox: Path) -> int:
    """The size of the tar the Session Runner limits: `tar_bytes`, the two empty blocks
    that end an archive, rounded up to tar's 10 KiB record. Only the size is modelled: a
    file or directory tar cannot read fails the platform's snapshot and is not reported
    here."""
    return -(-(tar_bytes(sandbox) + 1024) // 10240) * 10240


def run_step(payload: bytes, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    """One boundary, as `subprocess.run` would run it, except that the step is ended and
    waited for however this is left. `subprocess.run` does not wait after a Ctrl-C, which
    leaves the step unreaped for as long as the caller lives."""
    command = [sys.executable, "-m", "zetra_agents.local_step"]
    pipe = subprocess.PIPE
    with subprocess.Popen(command, stdin=pipe, stdout=pipe, stderr=pipe, cwd=cwd, env=env) as step:
        try:
            stdout, stderr = step.communicate(
                f"{len(payload)}\n".encode() + payload, timeout=BOUNDARY_SECONDS
            )
        except BaseException:
            step.kill()
            step.wait()
            raise
    return subprocess.CompletedProcess(command, step.returncode, stdout, stderr)


def snapshot_problems(size: int) -> list[str]:
    if size > MAX_SNAPSHOT:
        return [
            f"workspace/ and state/ reached about {size} bytes as a tar; the platform "
            + f"snapshots at most {MAX_SNAPSHOT} after every boundary and fails the session"
        ]
    return []


def report_problems(workspace: Path) -> list[str]:
    report = workspace / "report.md"
    if not report.is_file() or report.is_symlink():
        return ["the session completed without workspace/report.md, which is its result"]
    data = report.read_bytes()
    if len(data) > MAX_REPORT:
        return [f"report.md is {len(data)} bytes; the platform reads at most {MAX_REPORT}"]
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return ["report.md is not UTF-8"]
    return []


def run(
    agent: AgentDir | Path,
    scenario: Scenario | str | Path,
    *,
    live: bool = False,
    out: Path | None = None,
    max_boundaries: int | None = None,
    on_boundary: Callable[[dict], None] | None = None,
) -> Result:
    """One session: boundaries until the agent finishes, fails or reaches the limit."""
    if not isinstance(agent, AgentDir):
        agent = AgentDir(Path(agent))
    if not isinstance(scenario, Scenario):
        scenario = load_scenario(agent, scenario)
    problems = refusals(agent)
    if problems:
        raise ScenarioError("; ".join(problems))
    if live:
        if not os.environ.get(local_step.MODEL_URL):
            raise ScenarioError(
                f"--live needs {local_step.MODEL_URL}, the /v1 base URL of an OpenAI-compatible "
                f"endpoint ({local_step.MODEL_NAME} and {local_step.MODEL_API_KEY} are optional)"
            )
        scenario = replace(scenario, model=None)
    elif scenario.model is None:
        raise ScenarioError(
            "no model: give the scenario a `model:` script, or pass --live to use the "
            f"endpoint in {local_step.MODEL_URL}"
        )
    bundle = compiled(agent)
    limit = bundle["runtime"]["max_boundaries"]
    max_boundaries = limit if max_boundaries is None else max_boundaries
    if not 1 <= max_boundaries <= limit:
        raise ScenarioError(
            f"max boundaries is 1 to {limit}, this Agent's spec.runtime.maxBoundaries"
        )
    task = task_document(scenario.task)

    directory = new_directory(agent, out)
    app, sandbox, mcp_log = directory / "app", directory / "sandbox", directory / "mcp.jsonl"
    testing.lay_out(agent, app)
    sandbox.mkdir(mode=0o700)
    # The working directory: empty and read only, like the image's root filesystem.
    (directory / "cwd").mkdir(mode=0o500)
    mcp_log.touch(mode=0o600)
    spec = {
        "agent": agent.name,
        "grants": {backend: entry["tools"] for backend, entry in bundle["mcp"].items()},
        "mcp": list(scenario.mcp),
        "model": None if scenario.model is None else list(scenario.model),
        "mcpLog": str(mcp_log),
    }
    (directory / "run.json").write_text(
        json.dumps({"task": task, "scenario": scenario.source, **spec}, indent=2) + "\n",
        encoding="utf-8",
    )
    env = testing.child_environment(
        app,
        sandbox,
        AGENT_TOKENS_PER_SESSION=str(bundle["budgets"]["tokens_per_session"]),
        **{local_step.SPEC_ENV: str(directory / "run.json")},
        **{
            name: os.environ[name]
            for name in (local_step.MODEL_URL, local_step.MODEL_NAME, local_step.MODEL_API_KEY)
            if live and name in os.environ
        },
    )

    result = Result(agent.name, "boundary_limit", "live" if live else "scripted", directory)
    result.detail = f"not finished after {max_boundaries} boundaries"
    largest = 0
    _writable(app, False)
    try:
        for n in range(max_boundaries):
            payload = json.dumps({"boundary": n, "task": task}, separators=(",", ":")).encode()
            try:
                done = run_step(payload, directory / "cwd", env)
            except subprocess.TimeoutExpired:
                result.outcome = "timeout"
                result.detail = f"boundary {n} ran past {BOUNDARY_SECONDS:.0f} s"
                break
            stderr = done.stderr.decode("utf-8", "replace").strip()
            if stderr:
                (directory / f"boundary-{n:02d}.stderr").write_text(stderr + "\n", encoding="utf-8")
            outcome = OUTCOMES.get(done.returncode)
            # As the Session Runner reads a step: 0, 2, 3 and 4 mean what they say only
            # with the driver's result for this boundary, `done` agreeing with the exit
            # code. A tool that calls sys.exit(2) has not completed the session, and after
            # any other exit code no result is read at all.
            record = step_result(done.stdout, n, done.returncode) if outcome else None
            if record is None:
                result.outcome = {
                    local_step.SCRIPT_ENDED: "script_ended",
                    5: "protocol_error",
                }.get(done.returncode, "unknown")
                result.detail = _clip(stderr.splitlines()[-1] if stderr else "", 400) or (
                    f"boundary {n} exited {done.returncode} without a result for it"
                )
                break
            record["rc"] = done.returncode
            record["detail"] = _detail(record, _last_message(directory))
            result.boundaries.append(record)
            with (directory / "boundaries.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            if on_boundary:
                on_boundary(record)
            largest = max(largest, snapshot_size(sandbox))
            if outcome != "running":
                result.outcome, result.detail = outcome, record.get("summary", "")
                break
    finally:
        _writable(app, True)  # so the run directory can be removed

    result.mcp = [json.loads(line) for line in mcp_log.read_text(encoding="utf-8").splitlines()]
    result.problems = mcp_problems(agent, result.mcp)
    if "do not match its manifest" in result.detail:
        result.problems.append("agent.yaml `tools:` differs from the code; run `just render`")
    result.problems += snapshot_problems(largest)
    if result.outcome == "completed":
        result.problems += report_problems(sandbox / "workspace")
    report = sandbox / "workspace" / "report.md"
    if report.is_file() and not report.is_symlink():
        result.report = report  # in place: its links are relative
    (directory / "result.json").write_text(
        json.dumps(result.document(), indent=2) + "\n", encoding="utf-8"
    )
    return result
