"""One generic Temporal workflow for every agent, and the worker that serves it.

The workflow runs the same approval loop as the in-process runner; the only difference is
that a decision arrives as a Temporal Update. The Update's validator rejects, without
touching the history, a decision that names no pending operation, a payload digest other
than the one shown to the approver, an operation already decided, or an actor that is not a
named human; the handler re-runs the same checks because validators do not run on replay.
PydanticAI's ``TemporalDurability`` makes each model request and tool call an activity.
Requires the ``sdk`` extra (temporalio).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

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
from .runner import Decided, Decision, Proposal, RunError, arun_agent, expired, parse_duration

WORKFLOW_TYPE = "zetra.agent"
APPROVE_UPDATE = "approve"  # the Update name, for clients that address it by string


class RunRequest(BaseModel):
    agent: str
    input: dict[str, Any]
    principal: str = "unknown"


class RunResult(BaseModel):
    """The workflow completes with this even when the run failed in an expected way."""

    agent: str
    status: Literal["completed", "failed"] = "completed"
    output: dict[str, Any] | None
    error: RunError | None = None
    proposals: list[Proposal]
    decisions: dict[str, Decided]  # keyed by operation id


@dataclass
class Registered:
    bundle: Bundle
    agent: Agent[RunDeps, Any]


REGISTRY: dict[str, Registered] = {}


def register(bundle: Bundle, agent: Agent[RunDeps, Any]) -> Registered:
    """Register (or re-register) the agent the workflow runs for ``bundle.name``.

    Re-registering replaces the previous agent of that name, since activity names derive
    from the agent name and a worker refuses duplicates.
    """
    entry = Registered(bundle, agent)
    REGISTRY[bundle.name] = entry
    agents = ZetraAgentWorkflow.__pydantic_ai_agents__
    agents[:] = [a for a in agents if a.name != agent.name]
    agents.append(agent)
    return entry


@workflow.defn(name=WORKFLOW_TYPE)
class ZetraAgentWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__: list[
        Agent[Any, Any]
    ] = []  # populated by register() before the worker starts

    def __init__(self) -> None:
        self._decisions: dict[str, Decided] = {}
        self._pending: list[Proposal] = []

    def _check(self, decision: Decision) -> None:
        """Refuse anything but one fresh decision on exactly the payload the approver saw."""
        previous = self._decisions.get(decision.operation_id)
        if previous is not None:
            raise ApplicationError(
                f"operation {decision.operation_id!r} was already decided by {previous.actor}",
                type="AlreadyDecided",
                non_retryable=True,
            )
        pending = next((p for p in self._pending if p.operation_id == decision.operation_id), None)
        if pending is None:
            raise ApplicationError(
                f"nothing is pending approval for operation {decision.operation_id!r}",
                type="NotPending",
                non_retryable=True,
            )
        if decision.digest != pending.digest:
            raise ApplicationError(
                "the decision names a payload digest other than the one pending",
                type="DigestMismatch",
                non_retryable=True,
            )
        actor = decision.actor.strip()
        if not actor or actor == "none" or actor.startswith("policy:"):
            raise ApplicationError(
                "a human decision needs a named actor", type="InvalidActor", non_retryable=True
            )

    @workflow.update(name=APPROVE_UPDATE)
    def approve(self, decision: Decision) -> Decided:
        self._check(decision)  # validators are skipped on replay; the handler re-checks
        decided = Decided(
            **decision.model_dump() | {"actor": decision.actor.strip()}, recorded_at=workflow.now()
        )
        self._decisions[decision.operation_id] = decided
        return decided

    @approve.validator
    def validate_approval(self, decision: Decision) -> None:
        self._check(decision)

    @workflow.query
    def pending_approvals(self) -> list[Proposal]:
        return list(self._pending)

    @workflow.query
    def decisions(self) -> list[Decided]:
        return list(self._decisions.values())

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

        async def approver(pending: list[Proposal]) -> list[Decision]:
            self._pending = list(pending)
            ttl = min((parse_duration(p.ttl) for p in pending), default=timedelta(hours=24))
            try:
                await workflow.wait_condition(
                    lambda: all(p.operation_id in self._decisions for p in pending),
                    timeout=ttl,
                    timeout_summary="approval-expiry",
                )
            except TimeoutError:  # asyncio.TimeoutError is this builtin on 3.11+
                pass
            self._pending = []
            return [self._decisions.get(p.operation_id) or expired(p) for p in pending]

        outcome = await arun_agent(
            entry.agent, bundle, value, approver=approver, deps=deps, now=workflow.now
        )
        return RunResult(
            agent=bundle.name,
            status=outcome.status,
            output=outcome.output.model_dump(mode="json") if outcome.output is not None else None,
            error=outcome.error,
            proposals=outcome.proposals,
            decisions=outcome.decisions,
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
