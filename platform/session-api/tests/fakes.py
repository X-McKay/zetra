"""Fakes of the three seams, and the objects a start looks at."""

from __future__ import annotations

from datetime import UTC, datetime

from runner.types import SessionInput

from session_api.ports import RUNNING, Execution, SessionConflict, Unavailable

T0 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


class FakeSessions:
    def __init__(self) -> None:
        self.executions: dict[str, Execution] = {}
        self.inputs: dict[str, SessionInput] = {}
        self.submitted: list[tuple[SessionInput, dict]] = []
        self.cancelled: list[str] = []
        self.progress: object = {"state": "RUNNING_BOUNDARY", "boundaries": 2}
        self.down = False

    def add(self, inp: SessionInput, memo: dict | None, status: str = RUNNING, result=None):
        """A session that is already there, as if started some other way when memo is None."""
        self.inputs[inp.workflow_id] = inp
        self.executions[inp.workflow_id] = Execution(
            inp.workflow_id, status, T0, None, dict(memo or {}), result
        )

    async def start(self, inp, memo):
        if self.down:
            raise Unavailable("down")
        if inp.workflow_id in self.inputs:
            if self.inputs[inp.workflow_id] != inp:
                raise SessionConflict
            return False
        self.submitted.append((inp, memo))
        self.add(inp, memo)
        return True

    async def describe(self, workflow_id):
        return self.executions.get(workflow_id)

    async def query(self, workflow_id):
        if isinstance(self.progress, Exception):
            raise self.progress
        return self.progress

    async def input(self, workflow_id):
        return self.inputs.get(workflow_id)

    async def cancel(self, workflow_id):
        self.cancelled.append(workflow_id)
        return True

    async def list_running(self):
        return [e for e in self.executions.values() if e.status == RUNNING]


class FakeObjects:
    def __init__(self) -> None:
        self.agents: dict[str, dict] = {}
        self.releases: dict[str, dict] = {}

    def agent(self, namespace, name):
        return self.agents.get(name)

    def release(self, namespace, name):
        return self.releases.get(name)


class FakeReports:
    def __init__(self) -> None:
        self.reports: dict[str, str] = {}

    def read(self, snapshot):
        try:
            return self.reports[snapshot]
        except KeyError:
            raise ValueError("report missing") from None


def agent(
    name="db-admin", *, current="db-admin-r1", enabled=True, owned=True, mode="agent", held=None
):
    annotations = {}
    if owned:
        annotations["registry.agentplatform.io/deployment"] = "kubani"
    if current:
        annotations["registry.agentplatform.io/current-release"] = current
    if held:
        annotations["registry.agentplatform.io/held"] = held
    spec = {"identity": {"mode": mode}}
    if enabled is not None:
        spec["enabled"] = enabled
    return {"metadata": {"name": name, "annotations": annotations}, "spec": spec}


def release(name="db-admin-r1", *, of="db-admin", ready=True, declaration=None):
    spec = {"agent": of}
    if declaration is not None:
        spec["declaration"] = declaration
    condition = {"type": "Ready", "status": "True" if ready else "False"}
    if not ready:
        condition |= {"reason": "SignatureMissing", "message": "no signature"}
    return {"metadata": {"name": name}, "spec": spec, "status": {"conditions": [condition]}}
