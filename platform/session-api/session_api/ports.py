"""The three seams (`contract/sessions.md` section 6). The service decides; these only fetch.

Each has a real implementation in `real.py` and a fake in the tests. A real one raises
`Unavailable` when it could not reach what it wraps, and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from runner.types import SessionInput, SessionResult

RUNNING = "RUNNING"  # the one status name the service itself looks for


class Unavailable(Exception):
    """Temporal, the Kubernetes API or the artifact store could not be reached."""


class SessionConflict(Exception):
    """The session id exists with other input."""


@dataclass
class Execution:
    """What Temporal says about one workflow. `status` is the name of Temporal's status:
    RUNNING, COMPLETED, FAILED, CANCELED, TERMINATED, TIMED_OUT or CONTINUED_AS_NEW.
    `result` is set only for a completed execution whose result could be read."""

    workflow_id: str
    status: str
    start_time: datetime | None
    close_time: datetime | None
    memo: dict[str, Any] = field(default_factory=dict)
    result: SessionResult | None = None


class Sessions(Protocol):
    async def start(self, inp: SessionInput, memo: dict[str, Any]) -> bool:
        """Start the session; True when it was created, False when the same input is already
        there. Raises `SessionConflict` when the id exists with other input."""
        ...

    async def describe(self, workflow_id: str) -> Execution | None: ...

    async def query(self, workflow_id: str) -> Any:
        """The workflow's `status` query."""
        ...

    async def input(self, workflow_id: str) -> SessionInput | None:
        """The input in the workflow's first history event, or None when it cannot be read."""
        ...

    async def cancel(self, workflow_id: str) -> bool:
        """Ask for cancellation; False when the workflow had already closed."""
        ...

    async def list_running(self) -> list[Execution]:
        """Every running `SessionWorkflow` execution of the Temporal namespace."""
        ...


class Objects(Protocol):
    def agent(self, namespace: str, name: str) -> dict | None: ...

    def release(self, namespace: str, name: str) -> dict | None: ...


class Reports(Protocol):
    def read(self, snapshot: str) -> str:
        """The report of a snapshot. Raises ValueError or OSError when there is none."""
        ...
