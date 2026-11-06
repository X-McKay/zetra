"""Read-only T17 orchestration; an operator performs and receipts both scale changes."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sys
import time
import uuid
from dataclasses import asdict

from runner.conformance_admission import AdmissionSessions, KubernetesAdmission, ready, settled
from runner.conformance_fail_closed import (
    PHASES,
    RECEIPT_FIELDS,
    annotation_hash,
    empty,
    evaluate,
    read_evidence,
    restored,
)
from runner.submit import submit
from runner.types import SessionInput

NAMESPACE = "agent-platform"
DEPLOYMENT = "agent-policy-service"
CONTROL = "conformance-aps-outage-control"
LABEL = "app.kubernetes.io/name=agent-policy-service"


def receipt_progress(previous, current):
    for key, value in previous.items():
        if key not in current or current[key] != value:
            raise ValueError("operator receipt changed or removed prior evidence")
    if set(current) - (RECEIPT_FIELDS | set(PHASES)):
        raise ValueError("unexpected operator receipt field")
    return current


class OutageOps(KubernetesAdmission):
    """Fixed-resource get/list calls only; no mutation method is used by T17."""

    def __init__(self, core, custom, apps):
        super().__init__(core, custom, "tenant-test")
        self.apps = apps
        self.control_uid = None

    def receipt(self):
        obj = self.core.read_namespaced_config_map(CONTROL, NAMESPACE, _request_timeout=5)
        uid = obj.metadata.uid
        if not uid or self.control_uid not in (None, uid):
            raise ValueError("operator receipt ConfigMap replaced")
        self.control_uid = uid
        raw = (obj.data or {}).get("receipt.json", "")
        if len(raw.encode()) > 16384:
            raise ValueError("operator receipt exceeds limit")
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("invalid operator receipt")
        return document

    def sample(self):
        started = time.time_ns()
        deployment = self.apps.read_namespaced_deployment(DEPLOYMENT, NAMESPACE, _request_timeout=5)
        pods = self.core.list_namespaced_pod(
            NAMESPACE, label_selector=LABEL, _request_timeout=5
        ).items
        endpoints = self.core.read_namespaced_endpoints(DEPLOYMENT, NAMESPACE, _request_timeout=5)
        spec = self.core.api_client.sanitize_for_serialization(deployment.spec)
        spec.pop("replicas", None)
        pod_rows = []
        for pod in pods:
            statuses = pod.status.container_statuses or []
            pod_rows.append(
                {
                    "uid": pod.metadata.uid,
                    "name": pod.metadata.name,
                    "ready": not pod.metadata.deletion_timestamp
                    and pod.status.phase == "Running"
                    and bool(statuses)
                    and all(c.ready for c in statuses),
                    "image_id": ",".join(sorted(c.image_id or "" for c in statuses)),
                }
            )
        addresses = []
        for subset in endpoints.subsets or []:
            for available, group in ((True, subset.addresses), (False, subset.not_ready_addresses)):
                for address in group or []:
                    addresses.append(
                        {
                            "uid": address.target_ref.uid if address.target_ref else None,
                            "ready": available,
                        }
                    )
        return {
            "started_ns": started,
            "observed_ns": time.time_ns(),
            "uid": deployment.metadata.uid,
            "resource_version": deployment.metadata.resource_version,
            "generation": deployment.metadata.generation,
            "annotation_sha": annotation_hash(
                (deployment.metadata.annotations or {}).get("kustomize.toolkit.fluxcd.io/reconcile")
            ),
            "observed_generation": deployment.status.observed_generation,
            "replicas": deployment.spec.replicas,
            "ready_replicas": deployment.status.ready_replicas or 0,
            "spec_sha": hashlib.sha256(
                json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "pods": sorted(pod_rows, key=lambda p: p["uid"] or ""),
            "endpoints": sorted(addresses, key=lambda p: p["uid"] or ""),
        }


class FailClosedHarness:
    def __init__(
        self, client, ops, gateway_logs, release, marker, duration, artifact_dir, *, timeout=900
    ):
        self.client, self.ops, self.gateway_logs = client, ops, gateway_logs
        self.release, self.marker, self.duration = release, marker, duration
        self.artifact_dir, self.timeout = artifact_dir, timeout
        self.cleanup = AdmissionSessions(client, ops, timeout=180)
        self.observations = []
        self.receipt = {}
        self.notified = set()

    def notify(self, phase, **data):
        if phase not in self.notified:
            print(
                json.dumps({"test": "T17", "phase": phase, "marker": self.marker, **data}),
                file=sys.stderr,
                flush=True,
            )
            self.notified.add(phase)

    async def monitor(self, result, started):
        from runner.conformance import parse_gateway_log

        while True:
            sample = await asyncio.to_thread(self.ops.sample)
            self.observations.append(sample)
            current = await asyncio.to_thread(self.ops.receipt)
            self.receipt = receipt_progress(self.receipt, current)
            if sample["uid"] != self.receipt.get("deployment_uid"):
                raise ValueError("APS Deployment replaced")
            if "baseline_ready" not in self.notified:
                raw = await self.gateway_logs(time.time() - started + 5)
                records = [
                    r
                    for r in parse_gateway_log(raw)
                    if str(r.get("trace.id", "")).startswith(self.marker)
                ]
                traces = {
                    r["trace.id"]
                    for r in records
                    if r.get("http.status") == "200" and r.get("http.path") == "/kubernetes/mcp"
                }
                # Exactly the first 26 traffic slots span five seconds. This is an
                # operator signal only; complete final evidence still decides the test.
                if all(self.marker + f"{i:016x}" in traces for i in range(1, 27)) and restored(
                    sample
                ):
                    self.notify(
                        "baseline_ready",
                        deployment_uid=sample["uid"],
                        resource_version=sample["resource_version"],
                    )
            if self.receipt.get("down") and empty(sample):
                zeros = [
                    o
                    for o in self.observations
                    if o["started_ns"] >= self.receipt["down"]["acknowledged_ns"] and empty(o)
                ]
                if zeros and sample["observed_ns"] - zeros[0]["observed_ns"] >= 12_000_000_000:
                    self.notify(
                        "zero_confirmed_restore_now",
                        deployment_uid=sample["uid"],
                        resource_version=sample["resource_version"],
                    )
            if self.receipt.get("up") and restored(sample):
                self.notify(
                    "replicas_restored",
                    deployment_uid=sample["uid"],
                    resource_version=sample["resource_version"],
                )
            if result.done():
                return
            if time.time() - started > self.timeout:
                raise TimeoutError("T17 observation deadline")
            await asyncio.sleep(0.2)

    async def close(self, inp, handle, result, monitor):
        try:
            await settled(self.cleanup._close(inp, handle, result))
        finally:
            if not monitor.done():
                monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    async def t17(self):
        from runner.conformance import LOG_UNAVAILABLE, parse_gateway_log

        release, sample, receipt = await asyncio.gather(
            asyncio.to_thread(self.ops.release, self.release),
            asyncio.to_thread(self.ops.sample),
            asyncio.to_thread(self.ops.receipt),
        )
        expected = {
            "schema": "t17-v2",
            "marker": self.marker,
            "deployment_uid": sample["uid"],
            "original_replicas": 2,
            "original_generation": sample["generation"],
            "original_annotation_sha": sample["annotation_sha"],
        }
        if not ready(release) or release.get("spec", {}).get("agent") != "conformance-b":
            raise ValueError("T17 needs the explicit signed Ready conformance-b release")
        if receipt != expected or not restored(sample):
            raise ValueError("T17 needs a fresh prepared receipt and two Ready APS replicas")
        self.receipt = copy.deepcopy(receipt)
        self.observations = [sample]
        inp = SessionInput(
            "tenant-test",
            "conformance-b",
            self.release,
            str(uuid.uuid4()),
            None,
            {
                "tool": "probe_fail_closed",
                "args": {"marker": self.marker, "duration": str(self.duration)},
            },
        )
        started = time.time()
        handle = await submit(self.client, inp)
        result = asyncio.create_task(handle.result())
        monitor = asyncio.create_task(self.monitor(result, started))
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.gather(result, monitor)), self.timeout)
            final = result.result()
            probe = await asyncio.to_thread(read_evidence, self.artifact_dir, final.snapshot or "")
            raw = await self.gateway_logs(time.time() - started + 5)
            outcome = evaluate(
                probe=probe,
                marker=self.marker,
                duration=self.duration,
                receipt=self.receipt,
                observations=self.observations,
                gateway=parse_gateway_log(raw),
                logs_readable=LOG_UNAVAILABLE not in raw,
                session=asdict(final),
            )
            return {
                "id": "T17",
                **outcome,
                "session_id": inp.session_id,
                "snapshot": final.snapshot,
            }
        finally:
            await settled(self.close(inp, handle, result, monitor))
