"""Trusted, read-only budget observations and normal SessionWorkflow orchestration.

The gateway watcher never deletes or patches a pod. An authorized external
operator must replace the exact observed old pods within the fixture's120s pause.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from temporalio.api.enums.v1 import EventType

from runner.conformance_admission import AdmissionSessions, ready, settled
from runner.conformance_budget import (
    AGENTS,
    UUID,
    TRACE,
    budget_agent,
    evaluate_budget,
    read_budget_state,
)
from runner.submit import submit
from runner.types import SessionInput


def utcnow():
    # The guest records fractional seconds. Truncating the watch start can
    # incorrectly order a valid same-second HTTP completion after the watch.
    return datetime.now(UTC).isoformat(timespec="microseconds")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class BudgetObserver:
    def __init__(self, url, token_file):
        self.url, self.token_file = url.rstrip("/"), token_file

    def read(self, session, agent):
        with Path(self.token_file).open("rb") as source:
            raw = source.read(16385)
        if not 0 < len(raw) <= 16384:
            raise ValueError("invalid observer token file")
        token = raw.decode("ascii").strip()
        request = urllib.request.Request(
            self.url
            + "/v1/conformance/budgets/"
            + session
            + "?"
            + urllib.parse.urlencode({"agent": agent}),
            headers={"Authorization": "Bearer " + token},
        )
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=5) as response:
            body = response.read(8193)
        if len(body) > 8192:
            raise ValueError("oversize observation")
        value = json.loads(body)
        if (
            not isinstance(value, dict)
            or value.get("session_id") != session
            or value.get("agent") != agent
        ):
            raise ValueError("observation identity mismatch")
        return value


class KubernetesGatewayWatch:
    def __init__(self, core):
        self.core = core
        self.last_evidence = None

    def inventory(self):
        pods = self.core.list_namespaced_pod(
            "ai-gateway",
            label_selector="gateway.networking.k8s.io/gateway-name=agents",
            _request_timeout=10,
        ).items
        result = []
        configurations = []
        for pod in pods:
            statuses = pod.status.container_statuses or []
            result.append(
                {
                    "name": pod.metadata.name,
                    "uid": pod.metadata.uid,
                    "ready": bool(
                        pod.status.phase == "Running"
                        and not pod.metadata.deletion_timestamp
                        and statuses
                        and all(c.ready for c in statuses)
                    ),
                    "image_id": "|".join(sorted(c.image_id or "" for c in statuses)),
                }
            )
            # Hash config without emitting env values, command arguments or volume sources.
            config = self.core.api_client.sanitize_for_serialization(pod.spec)
            projection = {
                k: config.get(k)
                for k in (
                    "containers",
                    "initContainers",
                    "volumes",
                    "serviceAccountName",
                    "securityContext",
                    "runtimeClassName",
                )
            }
            configurations.append(
                hashlib.sha256(json.dumps(projection, sort_keys=True).encode()).hexdigest()
            )
        digest = hashlib.sha256(json.dumps(sorted(set(configurations))).encode()).hexdigest()
        return {"pods": sorted(result, key=lambda p: p["uid"]), "configuration": digest}

    async def replaced(self, before, *, agent, session_id):
        budget_agent("day", agent)
        if not isinstance(session_id, str) or not UUID.fullmatch(session_id):
            raise ValueError("exact budget session required")
        if len(before["pods"]) != 2 or not all(p["ready"] for p in before["pods"]):
            raise ValueError("two Ready gateway replicas required")
        started = utcnow()
        began = time.monotonic()
        emitted_ns = time.time_ns()
        old = {p["uid"] for p in before["pods"]}
        self.last_evidence = {
            "before": before["pods"],
            "after": [],
            "old_uids_absent": False,
            "configuration_before": before["configuration"],
            "configuration_after": None,
            "started_at": started,
            "emitted_ns": emitted_ns,
            "deadline_seconds": 100,
            "completed": False,
            "elapsed_seconds": 0.0,
        }
        print(
            json.dumps(
                {
                    "event": "T13 awaiting gateway replacement",
                    "test": "T13",
                    "agent": agent,
                    "session_id": session_id,
                    "old_pods": before["pods"],
                    "emitted_ns": emitted_ns,
                    "deadline_seconds": 100,
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        while time.monotonic() - began < 100:
            after = await asyncio.to_thread(self.inventory)
            current = {p["uid"] for p in after["pods"]}
            elapsed = time.monotonic() - began
            self.last_evidence.update(
                after=after["pods"],
                old_uids_absent=not bool(current & old),
                configuration_after=after["configuration"],
                elapsed_seconds=elapsed,
                last_observed_at=utcnow(),
            )
            # An API response arriving after the deadline cannot qualify a restart.
            if elapsed >= 100:
                break
            if (
                len(after["pods"]) == 2
                and not current & old
                and all(p["ready"] for p in after["pods"])
            ):
                self.last_evidence.update(completed=True, finished_at=utcnow())
                return dict(self.last_evidence)
            await asyncio.sleep(0.5)
        self.last_evidence.update(elapsed_seconds=time.monotonic() - began)
        raise TimeoutError("gateway replacement deadline")


async def recovery_from_history(events, converter, artifact_dir):
    scheduled = {}
    for event in events:
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            [arg] = await converter.decode(list(attrs.input.payloads), [dict])
            scheduled[event.event_id] = {
                "name": attrs.activity_type.name,
                "scheduled": event.event_id,
                "generation": arg.get("generation"),
                "boundary": arg.get("boundary"),
                "restore": arg.get("restore"),
            }
        elif event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            attrs = event.activity_task_completed_event_attributes
            values = await converter.decode(list(attrs.result.payloads))
            scheduled[attrs.scheduled_event_id].update(
                completed=event.event_id, result=values[0] if values else None
            )
    rows = list(scheduled.values())

    def one(name, generation, boundary=None):
        found = [
            r
            for r in rows
            if r["name"] == name
            and r.get("generation") == generation
            and (boundary is None or r.get("boundary") == boundary)
            and "completed" in r
        ]
        return found[0] if len(found) == 1 else {}

    deletion = one("delete_sandbox", 1)
    fence = one("set_generation", 2)
    creation = one("create_sandbox", 2)
    checkpoint = one("snapshot_workspace", 1, 4)
    previous = one("snapshot_workspace", 1, 3)
    ordered = (
        bool(deletion and fence and creation and checkpoint)
        and deletion.get("result") == "DELETED"
        and checkpoint["completed"]
        < deletion["scheduled"]
        < deletion["completed"]
        < fence["scheduled"]
        < fence["completed"]
        < creation["scheduled"]
        < creation["completed"]
    )
    restored = False
    if checkpoint and previous and creation.get("restore") == checkpoint.get("result"):
        before = read_budget_state(artifact_dir, previous["result"])
        zero = read_budget_state(artifact_dir, checkpoint["result"])
        restored = (
            before.get("usage") == 800
            and zero.get("usage") == 0
            and zero.get("next") == 5
            and zero.get("budget", {}).get("case") == "session"
        )
    return {
        "confirmed_delete_before_fence_create": ordered,
        "restored_committed_zero_checkpoint": restored,
        "event_ids": {
            name: row.get("completed")
            for name, row in (
                ("checkpoint", checkpoint),
                ("delete", deletion),
                ("fence", fence),
                ("create", creation),
            )
        },
    }


class BudgetHarness:
    def __init__(
        self,
        client,
        ops,
        observer,
        aps_logs,
        backend_logs,
        gateway,
        day_release,
        session_release,
        artifact_dir,
        *,
        timeout=900,
        day_agent=AGENTS["day"],
    ):
        if ops.tenant != "tenant-test":
            raise ValueError("budget fixtures require tenant-test")
        self.client, self.ops, self.observer = client, ops, observer
        self.aps_logs, self.backend_logs, self.gateway = aps_logs, backend_logs, gateway
        self.releases = {"day": day_release, "session": session_release}
        self.agents = {"day": budget_agent("day", day_agent), "session": AGENTS["session"]}
        self.decision_receipts = []
        self.artifact_dir, self.timeout = artifact_dir, timeout
        self.cleanup = AdmissionSessions(client, ops, timeout=180)
        self.observations = []

    async def validate(self, case):
        agent = self.agents[case]
        release = self.releases[case]
        if not release:
            raise ValueError("explicit signed budget release required")
        rel = await asyncio.to_thread(self.ops.release, release)
        obj = await asyncio.to_thread(self.ops.object, "agents", agent)
        models = obj.get("spec", {}).get("capabilities", {}).get("models", [])
        expected = {
            "tokensPerDay": 2000 if case == "day" else 2000000,
            "tokensPerSession": 200000 if case == "day" else 1000,
        }
        if (
            rel.get("spec", {}).get("agent") != agent
            or not ready(rel)
            or len(models) != 1
            or models[0].get("backend") != "llm-budget-conformance"
            or models[0].get("models") != ["conformance-byte-v1"]
            or models[0].get("budget") != expected
        ):
            raise ValueError("signed fixture or exact budget configuration mismatch")

    async def start(self, case, role):
        inp = SessionInput(
            "tenant-test",
            self.agents[case],
            self.releases[case],
            str(uuid.uuid4()),
            None,
            {"budget_case": role},
            max_boundaries=6,
        )
        handle = await submit(self.client, inp)
        result = asyncio.create_task(handle.result())
        monitor = asyncio.create_task(self.monitor(inp, handle, result))
        return inp, handle, result, monitor

    async def monitor(self, inp, handle, result):
        first = True
        last = None
        until = time.monotonic() + self.timeout
        while True:
            if time.monotonic() > until:
                raise TimeoutError("budget observation deadline")
            try:
                observation = await asyncio.to_thread(self.observer.read, inp.session_id, inp.agent)
            except urllib.error.HTTPError as exc:
                status = await handle.query("status")
                if (
                    first
                    and exc.code == 403
                    and status.get("state") in {"SUBMITTED", "PROVISIONING"}
                    and not result.done()
                ):
                    await asyncio.sleep(0.2)
                    continue
                raise RuntimeError("budget observer refused") from None
            key = (
                observation.get("session"),
                observation.get("day"),
                observation.get("generation"),
            )
            if key != last or result.done():
                self.observations.append(observation)
                last = key
            first = False
            if result.done():
                return
            await asyncio.sleep(0.2)

    async def finish(self, running):
        inp, handle, result, monitor = running
        await asyncio.wait_for(asyncio.shield(result), self.timeout)
        await monitor
        events = [event async for event in handle.fetch_history_events()]
        final = result.result()
        state = await asyncio.to_thread(read_budget_state, self.artifact_dir, final.snapshot or "")
        recovery = (
            await recovery_from_history(events, self.client.data_converter, self.artifact_dir)
            if inp.task["budget_case"] == "session"
            else {}
        )
        return {
            "session_id": inp.session_id,
            "release": inp.release,
            "case": inp.task["budget_case"],
            **asdict(final),
            "budget": state["budget"],
            "recovery": recovery,
        }

    async def close(self, running):
        inp, handle, result, monitor = running
        try:
            await settled(self.cleanup._close(inp, handle, result))
        finally:
            if not monitor.done():
                monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    async def close_all(self, active):
        async def collect():
            results = await asyncio.gather(
                *(self.close(running) for running in active), return_exceptions=True
            )
            failures = [r for r in results if isinstance(r, BaseException)]
            if failures:
                raise BaseExceptionGroup("budget session cleanup failed", failures)

        await settled(collect())

    async def wait_decision(self, session, reason, started, running):
        from runner.conformance import parse_aps_log

        until = time.monotonic() + self.timeout
        while time.monotonic() < until:
            if running[3].done() and running[3].exception():
                raise running[3].exception()
            records = parse_aps_log(await self.aps_logs(time.time() - started + 5))
            for record in records:
                if (
                    record.get("session_id") == session
                    and record.get("reason") == reason
                    and record.get("backend") == "llm-budget-conformance"
                    and record.get("agent_id") == "tenant-test/" + running[0].agent
                    and record.get("release") == running[0].release
                ):
                    # Keep only fixed-label/validated correlation metadata; never raw logs.
                    self.decision_receipts.append(
                        {
                            "session_id": session,
                            "reason": reason,
                            "backend": "llm-budget-conformance",
                            "decision_id": record.get("decision_id")
                            if isinstance(record.get("decision_id"), str)
                            and UUID.fullmatch(record["decision_id"])
                            else None,
                            "trace_id": record.get("trace_id")
                            if isinstance(record.get("trace_id"), str)
                            and TRACE.fullmatch(record["trace_id"])
                            else None,
                        }
                    )
                    return
            if running[2].done():
                raise RuntimeError("fixture ended before expected decision")
            await asyncio.sleep(0.5)
        raise TimeoutError("expected budget decision missing")

    async def run(self, case):
        from runner.conformance import parse_aps_log

        started = time.time()
        self.observations = []
        self.decision_receipts = []
        active, sessions = [], []
        restart = None
        barrier = None
        outcome = None
        phase = "fixture_validation"
        validated = False
        if case == "day":
            self.gateway.last_evidence = None
        try:
            await self.validate(case)
            validated = True
            if case == "day":
                phase = "gateway_inventory"
                before = await asyncio.to_thread(self.gateway.inventory)
                phase = "session_start"
                primary = await self.start(case, "day")
                active.append(primary)
                phase = "budget_denial"
                await self.wait_decision(
                    primary[0].session_id, "deny.budget_exhausted", started, primary
                )
                # APS logs the decision before the guest receives its response.
                # This pause coordinates signalling only; the final committed
                # HTTP finished_at must still precede the actual watch start.
                phase = "denial_response_barrier"
                barrier = {"seconds": 1, "started_at": utcnow(), "completed": False}
                await asyncio.sleep(1)
                barrier.update(completed=True, finished_at=utcnow())
                phase = "gateway_replacement"
                restart = await self.gateway.replaced(
                    before, agent=primary[0].agent, session_id=primary[0].session_id
                )
                phase = "session_completion"
                sessions = [await self.finish(primary)]
            else:
                phase = "control_start"
                control = await self.start(case, "control")
                active.append(control)
                phase = "control_allow"
                await self.wait_decision(control[0].session_id, "allow", started, control)
                phase = "session_start"
                primary = await self.start(case, "session")
                active.append(primary)
                phase = "session_completion"
                sessions = list(await asyncio.gather(self.finish(primary), self.finish(control)))
            phase = "aps_logs"
            aps = parse_aps_log(await self.aps_logs(time.time() - started + 5))
            phase = "backend_logs"
            backend = parse_aps_log(await self.backend_logs(time.time() - started + 5))
            phase = "evidence_evaluation"
            outcome = evaluate_budget(
                case=case,
                agent=self.agents[case],
                sessions=sessions,
                aps=aps,
                backend=backend,
                observations=self.observations,
                restart=restart,
            )
            outcome["evidence"]["sessions"] = [
                {
                    k: s.get(k)
                    for k in (
                        "session_id",
                        "case",
                        "outcome",
                        "generation",
                        "snapshot",
                        "release",
                        "recovery",
                    )
                }
                for s in sessions
            ]
            outcome["evidence"]["observations"] = list(self.observations)
            outcome["evidence"]["gateway_restart"] = restart
            outcome["evidence"]["denial_response_barrier"] = barrier
        except Exception as exc:
            # Freeze the evidence at failure. Later cleanup or operator activity
            # cannot rewrite this error into a pass. Avoid secondary network reads.
            outcome = {
                "result": "error",
                "error": type(exc).__name__,
                "phase": phase,
                "checks": {"fixture_validated": validated, "orchestration_completed": False},
                "evidence": {
                    "scope": "partial budget-path observations; no qualification verdict",
                    "agent": self.agents[case],
                    "failed_at": utcnow(),
                    "denial_response_barrier": copy.deepcopy(barrier),
                    "sessions": [
                        {
                            "session_id": r[0].session_id,
                            "release": r[0].release,
                            "case": r[0].task["budget_case"],
                        }
                        for r in active
                    ],
                    "observations": copy.deepcopy(self.observations),
                    "decision_receipts": copy.deepcopy(self.decision_receipts),
                    "gateway_restart": copy.deepcopy(
                        restart or (self.gateway.last_evidence if case == "day" else None)
                    ),
                },
            }
        finally:
            try:
                await self.close_all(active)
            except Exception as exc:
                if outcome is None:
                    raise
                outcome = {**outcome, "result": "error", "cleanup_error": type(exc).__name__}
        return {"id": "T13" if case == "day" else "T13b", **outcome}

    async def t13(self):
        return await self.run("day")

    async def t13b(self):
        return await self.run("session")
