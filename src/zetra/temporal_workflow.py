"""Deterministic orchestration only; agent execution is a retryable Activity.

Requires the optional temporal extra. Temporal's workflow sandbox is a
determinism helper, not an OS security sandbox.
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError


@workflow.defn
class AgentWorkflow:
    def __init__(self) -> None:
        self.stopped = False

    @workflow.signal
    def stop(self) -> None:
        self.stopped = True

    @workflow.run
    async def run(self, request: dict) -> dict:
        if self.stopped:
            raise ApplicationError("Workflow stopped", non_retryable=True)
        result = await workflow.execute_activity(
            "zetra.run_agent",
            request,
            start_to_close_timeout=timedelta(seconds=30),
            schedule_to_close_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(
                maximum_attempts=3, non_retryable_error_types=["Denied", "ContractError"]
            ),
        )
        if self.stopped:
            raise ApplicationError(
                "Workflow stopped; completed effect must be reconciled", non_retryable=True
            )
        return result
