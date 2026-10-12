"""Run an agent as a function from Input to Output, pausing for approvals.

The same loop serves the in-process runner and the Temporal workflow; only the approver
differs. A ``Proposal`` carries the exact payload the write tool built and validated before
it paused, so an approver sees what the receiver will commit; a ``Decision`` names that
proposal by operation id and digest, so a decision on a stale or altered payload cannot land.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.usage import UsageLimits

from .bundle import Bundle
from .contracts import Contract, RunDeps, canonical, digest
from .errors import Denied
from .receiver import Grant
from .tools import ToolDecl


class Proposal(BaseModel):
    """A write the agent wants to make, exactly as the receiver would commit it."""

    model_config = ConfigDict(frozen=True)

    operation_id: str
    tool: str
    capability: str
    payload: dict[str, Any]
    digest: str
    ttl: str
    tool_call_id: str


class Decision(BaseModel):
    """What an approver says about one proposal, naming the payload digest they looked at."""

    model_config = ConfigDict(frozen=True)

    operation_id: str
    digest: str
    approved: bool
    actor: str
    reason: str = ""


class Decided(Decision):
    """A decision once recorded, with the time it was recorded."""

    recorded_at: datetime


ErrorCode = Literal["budget_exhausted", "output_invalid", "model_error", "denied"]


class RunError(BaseModel):
    """Why a run ended without an output. These are expected ways for a run to end, not bugs."""

    model_config = ConfigDict(frozen=True)

    code: ErrorCode
    message: str


@dataclass
class RunOutcome:
    output: Any
    messages: list[ModelMessage]
    proposals: list[Proposal] = field(default_factory=list)
    decisions: dict[str, Decided] = field(default_factory=dict)  # keyed by operation id
    status: Literal["completed", "failed"] = "completed"
    error: RunError | None = None


AsyncApprover = Callable[[list[Proposal]], Awaitable[list[Decision]]]
SyncApprover = Callable[[list[Proposal]], list[Decision]]


def parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value)
    if not match or not any(match.groups()):
        raise Denied(f"invalid duration {value!r}")
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def prompt_for(value: Contract) -> str:
    render = getattr(value, "prompt", None)
    return render() if callable(render) else canonical(value.model_dump(mode="json"))


def usage_limits(bundle: Bundle) -> UsageLimits:
    budgets = bundle.budgets
    return UsageLimits(
        request_limit=int(budgets["model_requests"]), tool_calls_limit=int(budgets["tool_calls"])
    )


def make_deps(bundle: Bundle, value: Contract, *, run_id: str | None, principal: str) -> RunDeps:
    return RunDeps(
        run=run_id or f"local-{uuid.uuid4().hex[:12]}",
        release=f"sha256:{bundle.digest[:16]}",
        principal=principal,
        input=value.model_dump(mode="json"),
    )


def expired(proposal: Proposal) -> Decision:
    return Decision(
        operation_id=proposal.operation_id,
        digest=proposal.digest,
        approved=False,
        actor="none",
        reason="approval expired",
    )


class Session:
    """Drive one run step by step: start, inspect pending proposals, decide, repeat."""

    def __init__(
        self,
        agent: Agent[RunDeps, Any],
        bundle: Bundle,
        value: Contract,
        deps: RunDeps,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.agent, self.bundle, self.value, self.deps, self.now = agent, bundle, value, deps, now
        self.decls = {t.name: t for t in bundle.tools}
        self.limits = usage_limits(bundle)
        self.outcome = RunOutcome(output=None, messages=[])
        self.pending: list[Proposal] = []
        self._result: Any = None
        self._seq = 0

    @property
    def done(self) -> bool:
        return self.outcome.status == "failed" or (self._result is not None and not self.pending)

    async def _step(self, *args: Any, **kwargs: Any) -> None:
        """One agent.run; the expected ways it can end without an output become a RunError."""
        with capture_run_messages() as messages:
            try:
                self._result = await self.agent.run(
                    *args, deps=self.deps, usage_limits=self.limits, **kwargs
                )
            except UsageLimitExceeded as exc:
                self._fail("budget_exhausted", str(exc), messages)
            except UnexpectedModelBehavior as exc:
                self._fail("output_invalid", str(exc), messages)
            except ModelAPIError as exc:
                self._fail("model_error", str(exc), messages)
            except Denied as exc:
                self._fail("denied", str(exc), messages)
            else:
                self._collect()

    def _fail(self, code: ErrorCode, message: str, messages: list[ModelMessage]) -> None:
        self.pending = []
        self.outcome.status = "failed"
        self.outcome.error = RunError(code=code, message=message.splitlines()[0] or code)
        self.outcome.output = None
        self.outcome.messages = list(messages)

    async def start(self) -> None:
        await self._step(prompt_for(self.value))

    def _collect(self) -> None:
        """Turn the tool calls that paused for approval into proposals; the tool built each payload."""
        self.pending = []
        if isinstance(self._result.output, DeferredToolRequests):
            for part in self._result.output.approvals:
                decl: ToolDecl = self.decls[part.tool_name]
                built = self._result.output.metadata.get(part.tool_call_id) or {}
                if "payload" not in built or digest(built["payload"]) != built.get("digest"):
                    raise Denied(f"{decl.name} paused without a consistent payload and digest")
                self._seq += 1
                self.pending.append(
                    Proposal(
                        operation_id=f"{self.deps.run}:c{self._seq:06d}",
                        tool=decl.name,
                        capability=decl.capability,
                        payload=built["payload"],
                        digest=built["digest"],
                        ttl=decl.ttl or self.bundle.project.approval_ttl,
                        tool_call_id=part.tool_call_id,
                    )
                )
            self.outcome.proposals.extend(self.pending)
        else:
            self.outcome.output, self.outcome.messages = (
                self._result.output,
                self._result.all_messages(),
            )

    async def decide(self, decisions: list[Decision]) -> None:
        """Record one decision per pending proposal and resume the run with them."""
        if not self.pending:
            raise Denied("nothing is pending approval")
        by_operation = {d.operation_id: d for d in decisions}
        unknown = set(by_operation) - {p.operation_id for p in self.pending}
        if unknown:
            raise Denied(f"no proposal is pending for {sorted(unknown)}")
        approvals: dict[str, Any] = {}
        metadata: dict[str, dict[str, Any]] = {}
        for proposal in self.pending:
            decision = by_operation.get(proposal.operation_id) or expired(proposal).model_copy(
                update={"reason": "no decision"}
            )
            if decision.digest != proposal.digest:
                raise Denied(f"the decision on {proposal.operation_id} names a different payload")
            decided = (
                decision  # the workflow records decisions as they arrive, with their own time
                if isinstance(decision, Decided)
                else Decided(**decision.model_dump(), recorded_at=self.now())
            )
            self.outcome.decisions[proposal.operation_id] = decided
            if decided.approved:
                grant = Grant(
                    operation_id=proposal.operation_id,
                    digest=proposal.digest,
                    actor=decided.actor,
                    mode="human",
                    expires_at=decided.recorded_at + parse_duration(proposal.ttl),
                )
                approvals[proposal.tool_call_id] = True
                metadata[proposal.tool_call_id] = {"grant": grant.model_dump(mode="json")}
            else:
                reason = decided.reason or "no reason given"
                approvals[proposal.tool_call_id] = ToolDenied(f"approval denied: {reason}")
        await self._step(
            message_history=self._result.all_messages(),
            deferred_tool_results=DeferredToolResults(approvals=approvals, metadata=metadata),
        )


async def arun_agent(
    agent: Agent[RunDeps, Any],
    bundle: Bundle,
    value: Contract,
    *,
    approver: AsyncApprover,
    deps: RunDeps,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RunOutcome:
    session = Session(agent, bundle, value, deps, now=now)
    await session.start()
    while session.pending:
        await session.decide(await approver(session.pending))
    return session.outcome


def run_agent(
    agent: Agent[RunDeps, Any],
    bundle: Bundle,
    value: Contract,
    *,
    approver: SyncApprover,
    run_id: str | None = None,
    principal: str = "local",
) -> RunOutcome:
    async def ask(pending: list[Proposal]) -> list[Decision]:
        return approver(pending)

    deps = make_deps(bundle, value, run_id=run_id, principal=principal)
    return asyncio.run(arun_agent(agent, bundle, value, approver=ask, deps=deps))
