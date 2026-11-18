"""The session interface around the played cluster, with a played Temporal behind it.

Real: the session interface's application (`session_api.app`), its decisions and its routes,
called over HTTP in process by the evaluator's and the provisioner's own clients. Played: the
three seams of its contract section 6. `Objects` reads the played cluster, so a start is
refused for a release the controller has not made Ready. `PlayedTemporal` holds the sessions;
a test says how a session ends and reads what was started and cancelled. `Reports` is a table.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from runner.types import SessionInput, SessionResult
from session_api.app import create_app
from session_api.auth import Principal, StaticVerifier
from session_api.config import EVALUATOR, OPERATOR, PROVISIONER, Settings
from session_api.ports import RUNNING, Execution, SessionConflict
from loopworld.cluster import AV, Cluster

COMPLETED, FAILED, NEVER = "completed", "failed", "never"


@dataclass
class Ending:
    """How a session ends: completed with a report, failed, or never finishing."""

    how: str
    report: str | None = None


@dataclass
class Cancel:
    """A cancellation as it reached Temporal, and what the cluster said of the release then."""

    workflow_id: str
    release: str | None
    approval_then: Any


@dataclass
class PlayedTemporal:
    """Implements `session_api.ports.Sessions`. A session ends as `endings` says for its task's
    request text; a task with no ending never finishes."""

    now: Callable[[], datetime]
    approval_of: Callable[[str], Any]  # release name -> its spec.approval on the cluster now
    reports: dict[str, str] = field(default_factory=dict)
    endings: dict[str, Ending] = field(default_factory=dict)
    submitted: list[tuple[SessionInput, dict]] = field(default_factory=list)
    cancelled: list[Cancel] = field(default_factory=list)
    executions: dict[str, Execution] = field(default_factory=dict)
    inputs: dict[str, SessionInput] = field(default_factory=dict)
    _snapshots: Any = field(default_factory=lambda: itertools.count(1))

    def end(self, request: str, how: str, report: str | None = None) -> None:
        """The sessions started with this request text will end this way."""
        self.endings[request] = Ending(how, report)

    def finish(self, workflow_id: str, report: str | None = None) -> None:
        """A session that was running completes now."""
        self._close(workflow_id, "COMPLETED", report)

    def _close(self, workflow_id: str, status: str, report: str | None = None) -> None:
        old = self.executions[workflow_id]
        result = None
        if status == "COMPLETED":
            snapshot = f"snapshot-{next(self._snapshots)}"
            self.reports[snapshot] = report or ""
            result = SessionResult(outcome="COMPLETED", snapshot=snapshot)
        self.executions[workflow_id] = Execution(
            workflow_id, status, old.start_time, self.now(), old.memo, result
        )

    def started(self) -> list[dict[str, Any]]:
        """Every session start that created a session: the session, its release and task, and
        the memo the interface set."""
        return [
            {
                "sessionId": inp.session_id,
                "workflowId": inp.workflow_id,
                "release": inp.release,
                "task": inp.task,
                "purpose": memo.get("purpose"),
                "startedBy": memo.get("startedBy"),
            }
            for inp, memo in self.submitted
        ]

    def running(self) -> list[str]:
        return [w for w, e in self.executions.items() if e.status == RUNNING]

    # -- session_api.ports.Sessions --------------------------------------------------------

    async def start(self, inp: SessionInput, memo: dict[str, Any]) -> bool:
        if inp.workflow_id in self.inputs:
            if self.inputs[inp.workflow_id] != inp:
                raise SessionConflict
            return False
        self.submitted.append((inp, dict(memo)))
        self.inputs[inp.workflow_id] = inp
        self.executions[inp.workflow_id] = Execution(
            inp.workflow_id, RUNNING, self.now(), None, dict(memo)
        )
        request = inp.task.get("request") if isinstance(inp.task, dict) else None
        ending = self.endings.get(request)
        if ending is not None and ending.how == COMPLETED:
            self._close(inp.workflow_id, "COMPLETED", ending.report)
        elif ending is not None and ending.how == FAILED:
            self._close(inp.workflow_id, "FAILED")
        return True

    async def describe(self, workflow_id: str) -> Execution | None:
        return self.executions.get(workflow_id)

    async def query(self, workflow_id: str) -> Any:
        return {"state": "RUNNING_BOUNDARY"}

    async def input(self, workflow_id: str) -> SessionInput | None:
        return self.inputs.get(workflow_id)

    async def cancel(self, workflow_id: str) -> bool:
        ex = self.executions.get(workflow_id)
        if ex is None or ex.status != RUNNING:
            return False
        release = ex.memo.get("release")
        self.cancelled.append(Cancel(workflow_id, release, self.approval_of(release)))
        self._close(workflow_id, "CANCELED")
        return True

    async def list_running(self) -> list[Execution]:
        return [e for e in self.executions.values() if e.status == RUNNING]


class PlayedObjects:
    """Implements `session_api.ports.Objects` over the played cluster."""

    def __init__(self, cluster: Cluster) -> None:
        self.cluster = cluster

    def agent(self, namespace: str, name: str) -> dict | None:
        return self.cluster.get(AV, "Agent", namespace, name)

    def release(self, namespace: str, name: str) -> dict | None:
        return self.cluster.get(AV, "AgentRelease", namespace, name)


class PlayedReports:
    """Implements `session_api.ports.Reports`."""

    def __init__(self, table: dict[str, str]) -> None:
        self.table = table

    def read(self, snapshot: str) -> str:
        try:
            return self.table[snapshot]
        except KeyError:
            raise ValueError("report missing") from None


def session_tokens(cluster: str) -> dict[str, Principal]:
    """Tokens are names, as in the registry's table."""
    return {
        "operator": Principal("u-1", "al", frozenset({OPERATOR}), True, "al@example.com", ""),
        "evaluator": Principal(
            "sa-3",
            f"service-account-registry-evaluator-{cluster}",
            frozenset({EVALUATOR}),
            False,
            None,
            f"registry-evaluator-{cluster}",
        ),
        "provisioner": Principal(
            "sa-2",
            f"service-account-registry-provisioner-{cluster}",
            frozenset({PROVISIONER}),
            False,
            None,
            f"registry-provisioner-{cluster}",
        ),
    }


def session_app(
    tenant: str,
    cluster_name: str,
    cluster: Cluster,
    temporal: PlayedTemporal,
    clock: Callable[[], datetime],
):
    settings = Settings(tenant=tenant, cluster=cluster_name, artifact_root="/unused")
    return create_app(
        settings,
        StaticVerifier(session_tokens(cluster_name)),
        temporal,
        PlayedObjects(cluster),
        PlayedReports(temporal.reports),
        clock=clock,
    )
