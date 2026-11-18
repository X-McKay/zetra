"""Every decision of the session interface (`contract/sessions.md` section 4).

The routes call these; the seams in `ports.py` only fetch. Nothing here is written in terms of
Temporal or Kubernetes types, so the fakes exercise all of it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

from runner.types import NAME_RE, InvalidInput, SessionInput

from session_api.config import CURRENT_RELEASE_ANNOTATION, DEPLOYMENT_ANNOTATION, Settings
from session_api.errors import ApiError
from session_api.ports import (
    RUNNING,
    Execution,
    Objects,
    Reports,
    SessionConflict,
    Sessions,
)
from session_api.record import Entry

T = TypeVar("T")

PURPOSES = ("task", "evaluation")
# Temporal's status of a closed execution that did not complete -> the outcome shown.
CLOSED_OUTCOMES = {"CANCELED": "cancelled"}
RESULT_OUTCOMES = {
    "COMPLETED": "completed",
    "FAILED": "failed",
    "TIMED_OUT": "timed-out",
    "NOT_CONFIRMED": "not-confirmed",
}


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _state(ex: Execution) -> tuple[str, str | None]:
    """The two columns of the table in section 4.2."""
    if ex.status == RUNNING:
        return "running", None
    if ex.status == "COMPLETED" and ex.result is not None:
        return "finished", RESULT_OUTCOMES.get(ex.result.outcome, "failed")
    return "finished", CLOSED_OUTCOMES.get(ex.status, "failed")


class SessionService:
    def __init__(
        self, settings: Settings, sessions: Sessions, objects: Objects, reports: Reports
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.objects = objects
        self.reports = reports

    # -- helpers ---------------------------------------------------------------------------

    @staticmethod
    async def _remote(call: Awaitable[T], what: str) -> T:
        """A seam failed other than by an answer: the same reply whatever it was, so nothing
        of the error (a path, an address, a token) reaches the caller."""
        try:
            return await call
        except (ApiError, SessionConflict):
            raise
        except Exception:  # noqa: BLE001 - see above
            raise ApiError("unavailable", f"{what} could not be reached") from None

    async def _objects(self, fetch: Callable[[str, str], dict | None], name: str) -> dict | None:
        return await self._remote(
            asyncio.to_thread(fetch, self.settings.tenant, name), "the Kubernetes API"
        )

    def _workflow_id(self, agent: str, session_id: str) -> str:
        try:
            probe = SessionInput(self.settings.tenant, agent, "release", session_id, None, None)
            probe.validate()
        except InvalidInput as error:
            raise ApiError("invalid-arguments", str(error)) from None
        return SessionInput(
            self.settings.tenant, agent, "release", session_id, None, None
        ).workflow_id

    async def _meta(self, ex: Execution) -> dict[str, Any]:
        """release, purpose and startedBy of an execution: the memo, or for one started another
        way the release of the workflow's input, purpose `task` and no starter."""
        memo = ex.memo if isinstance(ex.memo, dict) else {}
        release = memo.get("release") if isinstance(memo.get("release"), str) else None
        if release is None:
            inp = await self._remote(self.sessions.input(ex.workflow_id), "Temporal")
            release = inp.release if inp is not None else None
        purpose = memo.get("purpose")
        started_by = memo.get("startedBy")
        return {
            "release": release,
            "purpose": purpose if purpose in PURPOSES else "task",
            "startedBy": started_by if isinstance(started_by, str) else None,
        }

    # -- 4.1 start -------------------------------------------------------------------------

    async def start(self, kind: str, who: str, body: Any, entry: Entry) -> tuple[dict, int]:
        entry.agent, entry.release, entry.purpose = body.agent, body.release, body.purpose
        if body.purpose not in PURPOSES:
            raise ApiError("invalid-arguments", "purpose must be task or evaluation")
        session_id = body.sessionId or str(uuid.uuid4())
        entry.session_id = session_id
        try:  # every rule of SessionInput.validate that does not need the resolved release
            SessionInput(
                self.settings.tenant,
                body.agent,
                body.release or "release",
                session_id,
                None,
                body.task,
                worker_pool=self.settings.worker_pool,
            ).validate()
        except InvalidInput as error:
            raise ApiError("invalid-arguments", str(error)) from None
        # 2
        if body.purpose != ("task" if kind == "operator" else "evaluation"):
            raise ApiError.forbidden(
                "an operator starts sessions with purpose task; the evaluator with evaluation"
            )
        ns = self.settings.tenant
        # 3
        agent = await self._objects(self.objects.agent, body.agent)
        if agent is None:
            raise ApiError("not-found", f"no Agent {body.agent} in tenant {ns}")
        # 4
        meta = agent.get("metadata") or {}
        annotations = meta.get("annotations") or {}
        if DEPLOYMENT_ANNOTATION not in annotations:
            raise ApiError(
                "wrong-path",
                f"{body.agent} is not owned by the registry; "
                "use the pull request path's run command",
            )
        # 5
        spec = agent.get("spec") or {}
        if spec.get("enabled") is False:
            raise ApiError("refused", "the agent is disabled")
        # 6
        current = annotations.get(CURRENT_RELEASE_ANNOTATION) or None
        name = body.release or current
        if name is None:
            raise ApiError("refused", "the agent has no current release")
        entry.release = name
        # 7
        if name != current and kind != "evaluator":
            raise ApiError.forbidden("only the evaluator may name a release that is not current")
        # 8
        release = await self._objects(self.objects.release, name)
        if release is None or (release.get("spec") or {}).get("agent") != body.agent:
            raise ApiError("not-found", f"no AgentRelease {name} of agent {body.agent}")
        # 9
        ready = next(
            (
                c
                for c in (release.get("status") or {}).get("conditions") or []
                if c.get("type") == "Ready"
            ),
            {},
        )
        if ready.get("status") != "True":
            why = ": ".join(
                str(part)
                for part in (ready.get("reason") or "Unknown", ready.get("message"))
                if part
            )
            raise ApiError("refused", f"release {name} is not Ready: {why}")
        # 10
        declaration = (release.get("spec") or {}).get("declaration")
        source = declaration if isinstance(declaration, dict) else spec
        if ((source.get("identity") or {}).get("mode")) != "agent":
            raise ApiError(
                "refused",
                "this agent acts for a person and needs that person's consent; "
                "it cannot be started here",
            )
        try:
            inp = SessionInput(
                ns,
                body.agent,
                name,
                session_id,
                None,
                body.task,
                worker_pool=self.settings.worker_pool,
            )
            inp.validate()
        except InvalidInput as error:
            raise ApiError("invalid-arguments", str(error)) from None
        memo = {"release": name, "startedBy": who, "purpose": body.purpose}
        try:
            created = await self._remote(self.sessions.start(inp, memo), "Temporal")
        except SessionConflict:
            raise ApiError("conflict", f"session {session_id} exists with other input") from None
        state = "running"
        if not created:
            existing = await self._remote(self.sessions.describe(inp.workflow_id), "Temporal")
            state = _state(existing)[0] if existing is not None else state
        answer = {
            "sessionId": session_id,
            "agent": body.agent,
            "release": name,
            "workflowId": inp.workflow_id,
            "state": state,
            "created": created,
        }
        return answer, 202 if created else 200

    # -- 4.2 read, 4.3 cancel one ------------------------------------------------------------

    async def _own(self, kind: str, agent: str, session_id: str, entry: Entry):
        """The execution and its meta, for a route that names one session."""
        entry.agent, entry.session_id = agent, session_id
        wid = self._workflow_id(agent, session_id)
        ex = await self._remote(self.sessions.describe(wid), "Temporal")
        if ex is None:
            raise ApiError("not-found", f"no session {session_id} of agent {agent}")
        meta = await self._meta(ex)
        entry.release, entry.purpose = meta["release"], meta["purpose"]
        if kind == "evaluator" and meta["purpose"] != "evaluation":
            raise ApiError.forbidden("the evaluator may only reach sessions of purpose evaluation")
        return ex, meta

    async def read(self, kind: str, agent: str, session_id: str, entry: Entry) -> dict:
        ex, meta = await self._own(kind, agent, session_id, entry)
        state, outcome = _state(ex)
        progress = None
        if state == "running":
            try:
                progress = await self.sessions.query(ex.workflow_id)
            except Exception:  # noqa: BLE001 - the query failing is not an error of the route
                progress = None
        report, problem = None, None
        if state == "finished":
            snapshot = ex.result.snapshot if ex.result is not None else None
            if snapshot is None:
                problem = "the session finished without a snapshot"
            else:
                try:
                    report = await asyncio.to_thread(self.reports.read, snapshot)
                except ValueError as error:
                    problem = f"the report could not be read: {error}"
                except Exception:  # noqa: BLE001 - a path or an address stays out of the answer
                    problem = "the report could not be read from the artifact store"
        return {
            "sessionId": session_id,
            "agent": agent,
            "release": meta["release"],
            "purpose": meta["purpose"],
            "startedBy": meta["startedBy"],
            "state": state,
            "outcome": outcome,
            "startedAt": _iso(ex.start_time),
            "finishedAt": _iso(ex.close_time),
            "progress": progress,
            "report": report,
            "reportProblem": problem,
        }

    async def cancel(self, kind: str, agent: str, session_id: str, entry: Entry) -> dict:
        ex, _ = await self._own(kind, agent, session_id, entry)
        requested = False
        if ex.status == RUNNING:
            requested = await self._remote(self.sessions.cancel(ex.workflow_id), "Temporal")
        return {"sessionId": session_id, "cancelRequested": requested}

    # -- 4.4 list, 4.5 cancel by release -----------------------------------------------------

    async def _running(self, agent: str | None, release: str | None) -> list[dict]:
        prefix = f"session:{self.settings.tenant}:" + (f"{agent}:" if agent else "")
        found = []
        for ex in await self._remote(self.sessions.list_running(), "Temporal"):
            parts = ex.workflow_id.split(":", 3)
            if ex.status != RUNNING or not ex.workflow_id.startswith(prefix) or len(parts) != 4:
                continue
            if not NAME_RE.fullmatch(parts[2]):
                continue
            meta = await self._meta(ex)
            if release is not None and meta["release"] != release:
                continue
            found.append(
                {
                    "sessionId": parts[3],
                    "agent": parts[2],
                    "release": meta["release"],
                    "purpose": meta["purpose"],
                    "startedBy": meta["startedBy"],
                    "startedAt": _iso(ex.start_time),
                    "_workflowId": ex.workflow_id,
                }
            )
        return sorted(found, key=lambda s: (s["startedAt"] or "", s["sessionId"]))

    async def list_running(self, state: str | None, agent: str | None, release: str | None) -> dict:
        if state != "running":
            raise ApiError("invalid-arguments", "state must be running")
        for label, value in (("agent", agent), ("release", release)):
            if value is not None and not NAME_RE.fullmatch(value):
                raise ApiError("invalid-arguments", f"{label} must be a DNS label")
        rows = await self._running(agent, release)
        return {"sessions": [{k: v for k, v in r.items() if k[0] != "_"} for r in rows]}

    async def cancel_release(self, agent: str, release: str, entry: Entry) -> dict:
        entry.agent, entry.release = agent, release
        for label, value in (("agent", agent), ("release", release)):
            if not NAME_RE.fullmatch(value):
                raise ApiError("invalid-arguments", f"{label} must be a DNS label")
        cancelled: list[str] = []
        failed: list[dict] = []
        for row in await self._running(agent, release):
            try:
                if await self.sessions.cancel(row["_workflowId"]):
                    cancelled.append(row["sessionId"])
            except Exception:  # noqa: BLE001 - one failing does not stop the others
                failed.append(
                    {"sessionId": row["sessionId"], "message": "the cancel could not be sent"}
                )
        return {"cancelled": cancelled, "failed": failed}
