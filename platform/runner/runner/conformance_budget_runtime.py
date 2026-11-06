"""Trusted, read-only budget observations and normal SessionWorkflow orchestration.

The gateway watcher never deletes or patches a pod. An authorized external
operator must replace the exact observed old pods within the fixture's120s pause.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict
from pathlib import Path

from temporalio.api.enums.v1 import EventType

from runner.conformance_admission import AdmissionSessions, ready, settled
from runner.conformance_budget import AGENTS, evaluate_budget, read_budget_state
from runner.submit import submit
from runner.types import SessionInput


def utcnow():
    from runner.conformance import utcnow as now

    return now()


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

    async def replaced(self, before):
        if len(before["pods"]) != 2 or not all(p["ready"] for p in before["pods"]):
            raise ValueError("two Ready gateway replicas required")
        started = utcnow()
        old = {p["uid"] for p in before["pods"]}
        print(
            json.dumps(
                {
                    "event": "T13 awaiting gateway replacement",
                    "old_pods": before["pods"],
                    "deadline_seconds": 100,
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        until = time.monotonic() + 100
        while time.monotonic() < until:
            after = await asyncio.to_thread(self.inventory)
            current = {p["uid"] for p in after["pods"]}
            if (
                len(after["pods"]) == 2
                and not current & old
                and all(p["ready"] for p in after["pods"])
            ):
                return {
                    "before": before["pods"],
                    "after": after["pods"],
                    "old_uids_absent": True,
                    "configuration_before": before["configuration"],
                    "configuration_after": after["configuration"],
                    "started_at": started,
                    "finished_at": utcnow(),
                }
            await asyncio.sleep(0.5)
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
    ):
        if ops.tenant != "tenant-test":
            raise ValueError("budget fixtures require tenant-test")
        self.client, self.ops, self.observer = client, ops, observer
        self.aps_logs, self.backend_logs, self.gateway = aps_logs, backend_logs, gateway
        self.releases = {"day": day_release, "session": session_release}
        self.artifact_dir, self.timeout = artifact_dir, timeout
        self.cleanup = AdmissionSessions(client, ops, timeout=180)
        self.observations = []

    async def validate(self, case):
        agent = AGENTS[case]
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
            AGENTS[case],
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

    async def wait_decision(self, session, reason, started, running):
        from runner.conformance import parse_aps_log

        until = time.monotonic() + self.timeout
        while time.monotonic() < until:
            if running[3].done() and running[3].exception():
                raise running[3].exception()
            records = parse_aps_log(await self.aps_logs(time.time() - started + 5))
            if any(
                r.get("session_id") == session
                and r.get("reason") == reason
                and r.get("backend") == "llm-budget-conformance"
                for r in records
            ):
                return
            if running[2].done():
                raise RuntimeError("fixture ended before expected decision")
            await asyncio.sleep(0.5)
        raise TimeoutError("expected budget decision missing")

    async def run(self, case):
        from runner.conformance import parse_aps_log

        await self.validate(case)
        started = time.time()
        self.observations = []
        active = []
        restart = None
        try:
            if case == "day":
                before = await asyncio.to_thread(self.gateway.inventory)
                primary = await self.start(case, "day")
                active.append(primary)
                await self.wait_decision(
                    primary[0].session_id, "deny.budget_exhausted", started, primary
                )
                restart = await self.gateway.replaced(before)
                sessions = [await self.finish(primary)]
            else:
                control = await self.start(case, "control")
                active.append(control)
                await self.wait_decision(control[0].session_id, "allow", started, control)
                primary = await self.start(case, "session")
                active.append(primary)
                sessions = list(await asyncio.gather(self.finish(primary), self.finish(control)))
            aps = parse_aps_log(await self.aps_logs(time.time() - started + 5))
            backend = parse_aps_log(await self.backend_logs(time.time() - started + 5))
            result = evaluate_budget(
                case=case,
                sessions=sessions,
                aps=aps,
                backend=backend,
                observations=self.observations,
                restart=restart,
            )
            result["evidence"]["sessions"] = [
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
            result["evidence"]["observations"] = self.observations
            result["evidence"]["gateway_restart"] = restart
            return {"id": "T13" if case == "day" else "T13b", **result}
        finally:
            for running in active:
                await self.close(running)

    async def t13(self):
        return await self.run("day")

    async def t13b(self):
        return await self.run("session")
