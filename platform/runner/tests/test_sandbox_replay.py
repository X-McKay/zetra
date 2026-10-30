"""Runs the first workflow task of SessionWorkflow in Temporal's real workflow sandbox.

No server is needed: the Replayer executes the workflow code against a hand-built history,
which proves the module imports and first task are sandbox-safe and deterministic to start.
"""

from __future__ import annotations

from google.protobuf.json_format import MessageToDict
from temporalio.client import WorkflowHistory
from temporalio.converter import DataConverter
from temporalio.worker import Replayer

from runner.types import SessionInput
from runner.workflow import CleanupReconcilerWorkflow, SessionWorkflow

SESSION = "6f1d2c3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"


def _history(workflow_type: str, queue: str, payload) -> WorkflowHistory:
    t = "2026-10-30T00:00:00Z"
    return WorkflowHistory.from_json(
        "wf",
        {
            "events": [
                {
                    "eventId": "1",
                    "eventTime": t,
                    "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED",
                    "workflowExecutionStartedEventAttributes": {
                        "workflowType": {"name": workflow_type},
                        "taskQueue": {"name": queue},
                        "input": {"payloads": [MessageToDict(payload)]},
                        "workflowExecutionTimeout": "29700s",
                        "workflowTaskTimeout": "10s",
                        "originalExecutionRunId": "r",
                        "firstExecutionRunId": "r",
                        "attempt": 1,
                    },
                },
                {
                    "eventId": "2",
                    "eventTime": t,
                    "eventType": "EVENT_TYPE_WORKFLOW_TASK_SCHEDULED",
                    "workflowTaskScheduledEventAttributes": {
                        "taskQueue": {"name": queue},
                        "startToCloseTimeout": "10s",
                        "attempt": 1,
                    },
                },
                {
                    "eventId": "3",
                    "eventTime": t,
                    "eventType": "EVENT_TYPE_WORKFLOW_TASK_STARTED",
                    "workflowTaskStartedEventAttributes": {"scheduledEventId": "2"},
                },
            ]
        },
    )


async def test_session_workflow_first_task_runs_in_sandbox():
    inp = SessionInput("tenant-a", "investigator", "r1", SESSION, "gh_1", {"goal": "x"})
    [payload] = await DataConverter.default.encode([inp])
    result = await Replayer(workflows=[SessionWorkflow]).replay_workflow(
        _history("SessionWorkflow", "sessions-tenant-a", payload)
    )
    assert result.replay_failure is None


async def test_reconciler_first_task_runs_in_sandbox():
    [payload] = await DataConverter.default.encode(["tenant-a"])
    result = await Replayer(workflows=[CleanupReconcilerWorkflow]).replay_workflow(
        _history("CleanupReconcilerWorkflow", "cleanup-tenant-a", payload)
    )
    assert result.replay_failure is None
