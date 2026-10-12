"""Run an agent as a function from Input to Output, pausing for approvals.

The same loop serves the in-process runner and the Temporal workflow: the only difference
is how the approver is asked. A pending approval carries the exact payload the pure builder
produced, so the approver sees what the receiver will commit.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import ModelMessage
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, ToolDenied
from pydantic_ai.usage import UsageLimits

from .bundle import Bundle
from .contracts import Contract, RunDeps, canonical
from .errors import Denied
from .receiver import Grant, payload_digest
from .tools import ToolDecl, preview_payload


@dataclass(frozen=True)
class Pending:
    tool_call_id: str
    tool: str
    operation_id: str
    args: dict[str, Any]
    payload: Contract
    digest: str
    ttl: str


@dataclass(frozen=True)
class Decision:
    approved: bool
    actor: str
    reason: str = ""


ErrorCode = Literal["budget_exhausted", "output_invalid", "model_error", "denied"]


@dataclass(frozen=True)
class RunError:
    """Why a run ended without an output. These are expected ways for a run to end, not bugs."""

    code: ErrorCode
    message: str


@dataclass
class RunOutcome:
    output: Any
    messages: list[ModelMessage]
    proposals: list[Pending] = field(default_factory=list)
    decisions: dict[str, Decision] = field(default_factory=dict)
    status: Literal["completed", "failed"] = "completed"
    error: RunError | None = None


AsyncApprover = Callable[[list[Pending]], Awaitable[dict[str, Decision]]]
SyncApprover = Callable[[list[Pending]], dict[str, Decision]]


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


class Session:
    """Drive one run step by step: start, inspect pending approvals, decide, repeat."""

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
        self.pending: list[Pending] = []
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
        self.outcome.error = RunError(code, message.splitlines()[0] if message else code)
        self.outcome.output = None
        self.outcome.messages = list(messages)

    async def start(self) -> None:
        await self._step(prompt_for(self.value))

    def _collect(self) -> None:
        self.pending = []
        if isinstance(self._result.output, DeferredToolRequests):
            for part in self._result.output.approvals:
                decl: ToolDecl = self.decls[part.tool_name]
                args = part.args_as_dict()
                payload = preview_payload(decl, args, self.deps, self.bundle.input_type)
                built = (self._result.output.metadata.get(part.tool_call_id) or {}).get("digest")
                if built is not None and built != payload_digest(payload):
                    raise Denied(
                        f"{decl.name}: the preview differs from the payload the tool built"
                    )
                self._seq += 1
                self.pending.append(
                    Pending(
                        tool_call_id=part.tool_call_id,
                        tool=decl.name,
                        operation_id=f"{self.deps.run}:c{self._seq:06d}",
                        args=args,
                        payload=payload,
                        digest=payload_digest(payload),
                        ttl=decl.ttl or self.bundle.project.approval_ttl,
                    )
                )
            self.outcome.proposals.extend(self.pending)
        else:
            self.outcome.output, self.outcome.messages = (
                self._result.output,
                self._result.all_messages(),
            )

    async def decide(self, decisions: dict[str, Decision]) -> None:
        if not self.pending:
            raise Denied("nothing is pending approval")
        approvals: dict[str, Any] = {}
        metadata: dict[str, dict[str, Any]] = {}
        for item in self.pending:
            decision = decisions.get(item.tool_call_id) or Decision(False, "none", "no decision")
            self.outcome.decisions[item.operation_id] = decision
            if decision.approved:
                grant = Grant(
                    operation_id=item.operation_id,
                    digest=item.digest,
                    actor=decision.actor,
                    mode="human",
                    expires_at=self.now() + parse_duration(item.ttl),
                )
                approvals[item.tool_call_id] = True
                metadata[item.tool_call_id] = {"grant": grant.model_dump(mode="json")}
            else:
                approvals[item.tool_call_id] = ToolDenied(decision.reason or "approval denied")
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
    async def ask(pending: list[Pending]) -> dict[str, Decision]:
        return approver(pending)

    deps = make_deps(bundle, value, run_id=run_id, principal=principal)
    return asyncio.run(arun_agent(agent, bundle, value, approver=ask, deps=deps))
