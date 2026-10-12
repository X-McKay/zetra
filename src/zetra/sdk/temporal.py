"""One generic Temporal workflow for every agent, and the worker that serves it.

The workflow runs the same approval loop as the in-process runner; the only difference is
that an approval arrives as a signal. PydanticAI's ``TemporalDurability`` makes each model
request and tool call an activity. Requires the ``sdk`` extra (temporalio).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin, PydanticAIWorkflow
from temporalio import workflow
from temporalio.client import Client, WorkflowHandle
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from .bundle import Bundle
from .contracts import RunDeps
from .runner import Decision, Pending, arun_agent, parse_duration

WORKFLOW_TYPE = "zetra.agent"


class RunRequest(BaseModel):
    agent: str
    input: dict[str, Any]
    principal: str = "unknown"


class ApprovalDecision(BaseModel):
    tool_call_id: str
    approved: bool
    actor: str
    reason: str = ""


class PendingApproval(BaseModel):
    tool_call_id: str
    tool: str
    operation_id: str
    payload: dict[str, Any]
    digest: str


class DecisionRecord(BaseModel):
    approved: bool
    actor: str
    reason: str = ""


class RunResult(BaseModel):
    agent: str
    output: dict[str, Any]
    proposals: list[PendingApproval]
    decisions: dict[str, DecisionRecord]  # keyed by operation id


@dataclass
class Registered:
    bundle: Bundle
    agent: Agent[RunDeps, Any]


REGISTRY: dict[str, Registered] = {}


def register(bundle: Bundle, agent: Agent[RunDeps, Any]) -> Registered:
    entry = Registered(bundle, agent)
    REGISTRY[bundle.name] = entry
    if agent not in ZetraAgentWorkflow.__pydantic_ai_agents__:
        ZetraAgentWorkflow.__pydantic_ai_agents__.append(agent)
    return entry


@workflow.defn(name=WORKFLOW_TYPE)
class ZetraAgentWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__: list[Agent[Any, Any]] = []  # noqa: RUF012 - populated by register() before the worker starts

    def __init__(self) -> None:
        self._decisions: dict[str, ApprovalDecision] = {}
        self._pending: list[PendingApproval] = []

    @workflow.signal
    def approve(self, decision: ApprovalDecision) -> None:
        self._decisions.setdefault(decision.tool_call_id, decision)  # first decision wins

    @workflow.query
    def pending_approvals(self) -> list[PendingApproval]:
        return list(self._pending)

    @workflow.run
    async def run(self, request: RunRequest) -> RunResult:
        entry = REGISTRY.get(request.agent)
        if entry is None:
            raise ApplicationError(
                f"no agent named {request.agent!r} on this worker", non_retryable=True
            )
        bundle = entry.bundle
        value = bundle.input_type.model_validate(request.input)
        deps = RunDeps(
            run=workflow.info().workflow_id,
            release=f"sha256:{bundle.digest[:16]}",
            principal=request.principal,
            input=value.model_dump(mode="json"),
        )

        async def approver(pending: list[Pending]) -> dict[str, Decision]:
            self._pending = [
                PendingApproval(
                    tool_call_id=p.tool_call_id,
                    tool=p.tool,
                    operation_id=p.operation_id,
                    payload=p.payload.model_dump(mode="json"),
                    digest=p.digest,
                )
                for p in pending
            ]
            ttl = min((parse_duration(p.ttl) for p in pending), default=timedelta(hours=24))
            try:
                await workflow.wait_condition(
                    lambda: all(p.tool_call_id in self._decisions for p in pending),
                    timeout=ttl,
                    timeout_summary="approval-expiry",
                )
            except TimeoutError:
                pass
            self._pending = []
            decisions: dict[str, Decision] = {}
            for p in pending:
                decided = self._decisions.get(p.tool_call_id)
                decisions[p.tool_call_id] = (
                    Decision(decided.approved, decided.actor, decided.reason)
                    if decided
                    else Decision(False, "none", "approval expired")
                )
            return decisions

        outcome = await arun_agent(
            entry.agent, bundle, value, approver=approver, deps=deps, now=workflow.now
        )
        return RunResult(
            agent=bundle.name,
            output=outcome.output.model_dump(mode="json"),
            proposals=[
                PendingApproval(
                    tool_call_id=p.tool_call_id,
                    tool=p.tool,
                    operation_id=p.operation_id,
                    payload=p.payload.model_dump(mode="json"),
                    digest=p.digest,
                )
                for p in outcome.proposals
            ],
            decisions={
                op: DecisionRecord(approved=d.approved, actor=d.actor, reason=d.reason)
                for op, d in outcome.decisions.items()
            },
        )


def workflow_runner(bundles: list[Bundle]) -> SandboxedWorkflowRunner:
    """Pass the SDK and the agent packages through the sandbox so the workflow sees the registry."""
    modules = ["zetra", *(b.package_dir.name for b in bundles)]
    return SandboxedWorkflowRunner(
        restrictions=SandboxRestrictions.default.with_passthrough_modules(*modules)
    )


async def connect(address: str, namespace: str = "default") -> Client:
    return await Client.connect(address, namespace=namespace, plugins=[PydanticAIPlugin()])


def worker(client: Client, task_queue: str, entries: list[Registered]) -> Worker:
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[ZetraAgentWorkflow],
        workflow_runner=workflow_runner([e.bundle for e in entries]),
    )


async def start_run(
    client: Client, task_queue: str, request: RunRequest, *, workflow_id: str
) -> WorkflowHandle[Any, RunResult]:
    return await client.start_workflow(
        WORKFLOW_TYPE,
        request,
        id=workflow_id,
        task_queue=task_queue,
        result_type=RunResult,
    )
