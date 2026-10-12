"""``zetra new | check | run | dev | worker`` for SDK packages.

Argument parsing lives in :mod:`zetra.cli` so the core CLI works without the ``sdk`` extra;
this module only executes the parsed commands.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from .assemble import assemble
from .bundle import Bundle, build, lock_status, write_lock
from .errors import CompileError, Denied
from .receiver import SqliteReceiver
from .runner import Decision, Proposal, run_agent
from .tools import Bindings

SPEC_TEMPLATE = """name: {name}
description: Replace with the agent's bounded purpose.
model: approved:balanced-v1
instructions: |
  Describe what the agent does, what it must never do, and when to stop.
capabilities: []
"""
CONTRACTS_TEMPLATE = """from typing import Literal

from pydantic import Field

from zetra.sdk import Contract


class Request(Contract):
    text: str = Field(max_length=500)


class Reply(Contract):
    kind: Literal["reply"] = "reply"
    text: str


Input, Output = Request, Reply
"""
NOTE_CONTRACT = """

class Note(Contract):
    text: str = Field(max_length=1000)
"""
TEST_TEMPLATE = """from {package}.contracts import Reply, Request

from zetra.sdk.testing import final


def test_runs_and_returns_valid_output(agent):
    agent.model.script(final(Reply(text="hello")))
    assert agent.run(Request(text="hi")).output.text == "hello"
"""
WRITE_TOOL_TEMPLATE = '''from zetra.sdk import approval, tool

from ..contracts import Note, Request


@approval
@tool("notes.write")
def record_note(text: str, *, request: Request) -> Note:
    """Record one note; a human approves this exact payload before it is committed."""
    return Note(text=text)
'''


def new(root: Path, *, write: bool) -> dict[str, Any]:
    root = root.resolve()
    if root.exists() and any(root.iterdir()):
        raise CompileError("ZT-NEW-001", "new requires a new or empty directory")
    if not root.name.isidentifier() or root.name != root.name.lower():
        raise CompileError(
            "ZT-PKG-001", "the agent directory must be an importable lowercase package name"
        )
    package = root.name
    (root / "tests").mkdir(parents=True)
    spec = SPEC_TEMPLATE.format(name=package.replace("_", "-"))
    (root / "agent.yaml").write_text(spec, encoding="utf-8")
    contracts = CONTRACTS_TEMPLATE
    if write:
        contracts += NOTE_CONTRACT
        (root / "tools").mkdir()
        (root / "tools" / "record_note.py").write_text(WRITE_TOOL_TEMPLATE, encoding="utf-8")
    (root / "contracts.py").write_text(contracts, encoding="utf-8")
    test = TEST_TEMPLATE.format(package=package)
    (root / "tests" / f"test_{package}.py").write_text(test, encoding="utf-8")
    return {
        "status": "created",
        "directory": str(root),
        "next": [f"zetra check {root}", f"pytest {root}"],
    }


def check(root: Path, *, frozen: bool) -> tuple[dict[str, Any], int]:
    bundle = build(root)
    status = lock_status(bundle)
    if status != "current":
        if frozen:
            report = {
                "agent": bundle.name,
                "lock": status,
                "error": "zetra.lock is not current; run zetra check without --frozen",
            }
            return report, 1
        write_lock(bundle)
    lock = bundle.lock()
    keys = ("capability", "effect", "approval")
    return {
        "agent": bundle.name,
        "tier": lock["tier"],
        "capabilities": lock["capabilities"],
        "tools": {n: {k: v for k, v in t.items() if k in keys} for n, t in lock["tools"].items()},
        "mcp": lock["mcp"],
        "skills": sorted(lock["skills"]),
        "lock": "written" if status != "current" else "current",
        "bundle": f"sha256:{bundle.digest}",
    }, 0


def _receivers(bundle: Bundle) -> Bindings:
    state = bundle.package_dir / ".zetra" / "receivers"
    return Bindings(
        {
            t.resource: SqliteReceiver(t.resource, state / f"{t.resource}.db")
            for t in bundle.tools
            if t.is_write
        }
    )


def _decide(mode: str, proposal: Proposal) -> Decision:
    """Show the exact payload a receiver would commit, then decide per ``--approve``."""
    print(f"\napproval needed: {proposal.tool}  operation {proposal.operation_id}", file=sys.stderr)
    print(json.dumps(proposal.payload, indent=2), file=sys.stderr)
    print(f"digest sha256:{proposal.digest}", file=sys.stderr)
    approved, actor, reason = False, f"cli:{mode}", ""
    if mode == "auto":
        approved = True
    elif mode == "deny":
        reason = "denied by --approve deny"
    else:
        try:
            approved = input("approve this exact payload? [y/N] ").strip().lower() == "y"
        except EOFError:
            approved = False
        reason = "" if approved else "denied at prompt"
    return Decision(
        operation_id=proposal.operation_id,
        digest=proposal.digest,
        approved=approved,
        actor=actor,
        reason=reason,
    )


def _require_terminal(mode: str) -> None:
    if mode == "prompt" and not sys.stdin.isatty():
        raise Denied(
            "--approve prompt needs a terminal to answer in; use --approve auto or --approve deny"
        )


def _approver(mode: str):
    def decide(pending: list[Proposal]) -> list[Decision]:
        return [_decide(mode, proposal) for proposal in pending]

    return decide


def _model(name: str | None) -> Any:
    if name == "test":
        from pydantic_ai.models.test import TestModel

        return TestModel()
    return name


def _stubs(bundle: Bundle, model: str | None) -> dict[str, Any] | None:
    """With ``--model test`` nothing leaves the process: gateway servers answer canned text."""
    if model != "test" or not bundle.mcp:
        return None
    from .testing import StubGateway

    stubs: dict[str, Any] = {}
    for server, tools in bundle.mcp.items():
        gateway = StubGateway(server)
        for tool in tools:
            gateway.stub(tool, f"stubbed {server}.{tool} result")
        stubs[server] = gateway.toolset()
    return stubs


def run(root: Path, raw_input: str, *, approve: str, model: str | None) -> dict[str, Any]:
    _require_terminal(approve)
    bundle = build(root)
    value = bundle.input_type.model_validate(json.loads(raw_input))
    agent = assemble(
        bundle, _receivers(bundle), model=_model(model), gateway_stubs=_stubs(bundle, model)
    )
    outcome = run_agent(agent, bundle, value, approver=_approver(approve), principal="cli:local")
    return {
        "agent": bundle.name,
        "status": outcome.status,
        "output": outcome.output.model_dump(mode="json") if outcome.output is not None else None,
        "error": outcome.error.model_dump() if outcome.error else None,
        "proposals": [p.model_dump() for p in outcome.proposals],
        "decisions": {k: d.model_dump(mode="json") for k, d in outcome.decisions.items()},
        "mode": "in-process; cooperative controls only; not durable, not isolated",
    }


def _temporal_cli() -> str | None:
    """``zetra dev`` uses an installed Temporal CLI when one is on PATH or named by ZETRA_TEMPORAL_CLI.

    Otherwise temporalio downloads one from temporal.download on first use.
    """
    return os.environ.get("ZETRA_TEMPORAL_CLI") or shutil.which("temporal")


async def _dev(
    root: Path, raw_input: str, *, approve: str, model: str | None, timeout: float
) -> dict[str, Any]:
    """Run once on a throwaway local Temporal dev server, then replay the history."""
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer

    from . import temporal as t

    _require_terminal(approve)
    bundle = build(root)
    value = bundle.input_type.model_validate(json.loads(raw_input))
    agent = assemble(
        bundle,
        _receivers(bundle),
        model=_model(model),
        durable=True,
        gateway_stubs=_stubs(bundle, model),
    )
    entry = t.register(bundle, agent)
    request = t.RunRequest(
        agent=bundle.name, input=value.model_dump(mode="json"), principal="cli:dev"
    )
    task_queue = "zetra-dev"
    env_ = await WorkflowEnvironment.start_local(
        plugins=[t.PydanticAIPlugin()], dev_server_existing_path=_temporal_cli()
    )
    async with env_ as env:
        async with t.worker(env.client, task_queue, [entry]):
            handle = await t.start_run(
                env.client,
                task_queue,
                request,
                workflow_id=f"dev:{bundle.name}:{uuid.uuid4().hex[:8]}",
            )
            decided: set[str] = set()
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                if asyncio.get_running_loop().time() > deadline:
                    await handle.terminate("zetra dev timeout")
                    raise TimeoutError(
                        f"run did not finish within {timeout:g}s; "
                        f"last pending activity: {await _pending_activity(handle)}"
                    )
                for p in await handle.query(t.ZetraAgentWorkflow.pending_approvals):
                    if p.operation_id in decided:
                        continue
                    decided.add(p.operation_id)
                    recorded = await handle.execute_update(
                        t.APPROVE_UPDATE, _decide(approve, p), result_type=t.Decided
                    )
                    verdict = "approved" if recorded.approved else "denied"
                    print(f"recorded: {verdict} by {recorded.actor}", file=sys.stderr)
                try:
                    result = await asyncio.wait_for(asyncio.shield(handle.result()), timeout=0.5)
                    break
                except TimeoutError:
                    continue
            history = await handle.fetch_history()
        replayer = Replayer(
            workflows=[t.ZetraAgentWorkflow],
            plugins=[t.PydanticAIPlugin()],
            workflow_runner=t.workflow_runner([bundle]),
        )
        await replayer.replay_workflow(history)
    return {
        "agent": bundle.name,
        "workflow_id": handle.id,
        "status": result.status,
        "output": result.output,
        "error": result.error.model_dump() if result.error else None,
        "proposals": [p.model_dump() for p in result.proposals],
        "decisions": {k: v.model_dump(mode="json") for k, v in result.decisions.items()},
        "history_events": len(history.events),
        "replay": "passed",
        "mode": "local Temporal dev server; per-call activities; cooperative controls only",
    }


async def _pending_activity(handle: Any) -> str:
    description = await handle.describe()
    for activity in description.raw_description.pending_activities:
        failure = activity.last_failure.message if activity.HasField("last_failure") else ""
        cause = activity.last_failure.cause.message if failure else ""
        return f"{activity.activity_type.name} attempt {activity.attempt}: {cause or failure}"
    return "none"


async def _worker(
    directories: list[str], *, address: str, namespace: str, task_queue: str, model: str | None
) -> None:
    from . import temporal as t

    entries = []
    for directory in directories:
        bundle = build(Path(directory))
        agent = assemble(bundle, _receivers(bundle), model=_model(model), durable=True)
        entries.append(t.register(bundle, agent))
    client = await t.connect(address, namespace)
    names = [e.bundle.name for e in entries]
    print(f"serving {names} on {task_queue} at {address}", file=sys.stderr)
    await t.worker(client, task_queue, entries).run()


def handle(args: argparse.Namespace) -> int:
    try:
        if args.command == "new":
            print(json.dumps(new(Path(args.directory), write=args.write), indent=2))
            return 0
        if args.command == "check":
            report, code = check(Path(args.directory), frozen=args.frozen)
            print(json.dumps(report, indent=2))
            return code
        if args.command == "run":
            report = run(Path(args.directory), args.input, approve=args.approve, model=args.model)
            print(json.dumps(report, indent=2))
            return 0 if report["status"] == "completed" else 3
        if args.command == "dev":
            report = asyncio.run(
                _dev(
                    Path(args.directory),
                    args.input,
                    approve=args.approve,
                    model=args.model,
                    timeout=args.timeout,
                )
            )
            print(json.dumps(report, indent=2))
            return 0 if report["status"] == "completed" else 3
        if args.command == "worker":
            asyncio.run(
                _worker(
                    args.directories,
                    address=args.address,
                    namespace=args.namespace,
                    task_queue=args.task_queue,
                    model=args.model,
                )
            )
            return 0
    except (CompileError, Denied, ValueError, OSError) as exc:
        print(f"zetra: {exc}", file=sys.stderr)
        return 1
    return 2
