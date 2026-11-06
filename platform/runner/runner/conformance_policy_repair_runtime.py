"""Exclusive T28: one owned allow-all policy, real session, no manual reconcile."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from kubernetes.client.exceptions import ApiException
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError
from temporalio.service import RPCError, RPCStatusCode

from runner.conformance_admission import settled

from runner.activities import EXPECTED_POLICIES, netpol_digest
from runner.conformance import failure_message
from runner.conformance_policy_repair import evaluate
from runner.submit import submit
from runner.types import SessionInput

CONTROLLER_LABEL = "app.kubernetes.io/name=registry-controller"
MISMATCH = "effective NetworkPolicy set differs from the controller's"


class PolicyRepairOps:
    def __init__(self, core, net, custom, expected_digest):
        self.core, self.net, self.custom = core, net, custom
        self.expected_digest = expected_digest
        self.tenant = "tenant-test"

    def snapshot(self):
        policies = self.net.list_namespaced_network_policy(self.tenant, _request_timeout=5).items
        namespace = self.core.read_namespace(self.tenant, _request_timeout=5)
        names, digest = netpol_digest(policies, self.core.api_client.sanitize_for_serialization)
        published = (namespace.metadata.annotations or {}).get("agentplatform.io/netpol-digest")
        return {
            "names": sorted(names),
            "digest": digest,
            "published_digest": published,
            "valid": names == EXPECTED_POLICIES and digest == published == self.expected_digest,
            "uids": {p.metadata.name: p.metadata.uid for p in policies},
        }

    def pods(self, session=None):
        label = "agentplatform.io/sandbox=true"
        if session:
            label += ",agentplatform.io/session=" + session
        return self.core.list_namespaced_pod(
            self.tenant, label_selector=label, _request_timeout=5
        ).items

    def gate(self, agent, release):
        rel = self.custom.get_namespaced_custom_object(
            "agentplatform.io",
            "v1alpha1",
            self.tenant,
            "agentreleases",
            release,
            _request_timeout=5,
        )
        if (
            rel.get("spec", {}).get("agent") != agent
            or rel.get("status", {}).get("ready") is not True
            or rel.get("status", {}).get("identityMode") != "agent"
        ):
            raise ValueError("T28 requires exact Ready agent-mode release")
        if self.pods():
            raise ValueError("T28 requires an exclusive tenant with no sandbox Pods")

    def controller(self):
        pods = self.core.list_namespaced_pod(
            "agent-platform", label_selector=CONTROLLER_LABEL, _request_timeout=5
        ).items
        result = []
        for pod in pods:
            statuses = pod.status.container_statuses or []
            if (
                pod.metadata.deletion_timestamp
                or pod.status.phase != "Running"
                or not statuses
                or not all(s.ready for s in statuses)
            ):
                raise ValueError("controller not stable Ready")
            result.append(
                {
                    "name": pod.metadata.name,
                    "uid": pod.metadata.uid,
                    "image_id": "|".join(sorted(s.image_id or "" for s in statuses)),
                    "restarts": sum(s.restart_count for s in statuses),
                }
            )
        if len(result) != 1 or not result[0]["image_id"]:
            raise ValueError("one pinned Ready controller required")
        return result

    def create(self, name):
        create_error = None
        try:
            policy = self.net.create_namespaced_network_policy(
                self.tenant,
                {
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "NetworkPolicy",
                    "metadata": {"name": name, "labels": {"agentplatform.io/conformance": name}},
                    "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]},
                },
                _request_timeout=5,
            )
        except Exception as exc:
            # Recover only this unguessable run-owned object after an ambiguous
            # create response, so cleanup can still use its observed exact UID.
            create_error = type(exc).__name__
            policy = self.net.read_namespaced_network_policy(name, self.tenant, _request_timeout=5)
            if (policy.metadata.labels or {}).get("agentplatform.io/conformance") != name:
                raise ValueError("unowned T28 policy; refuse cleanup") from None
        return {
            "name": policy.metadata.name,
            "uid": policy.metadata.uid,
            "resource_version": policy.metadata.resource_version,
            "creation_confirmed": create_error is None,
        }

    def recover_owned(self, name):
        try:
            policy = self.net.read_namespaced_network_policy(name, self.tenant, _request_timeout=5)
        except ApiException as exc:
            if exc.status == 404:
                return None
            raise
        if (policy.metadata.labels or {}).get("agentplatform.io/conformance") != name:
            raise ValueError("unowned T28 policy; refuse cleanup")
        return {
            "name": policy.metadata.name,
            "uid": policy.metadata.uid,
            "resource_version": policy.metadata.resource_version,
            "creation_confirmed": False,
        }

    def remove_owned(self, extra):
        try:
            current = self.net.read_namespaced_network_policy(
                extra["name"], self.tenant, _request_timeout=5
            )
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise
        if current.metadata.uid != extra["uid"]:
            raise ValueError("T28 policy replaced; refuse cleanup")
        self.net.delete_namespaced_network_policy(
            extra["name"],
            self.tenant,
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": extra["uid"]},
            },
            _request_timeout=5,
        )
        return True

    def receipts(self, controller, extra):
        # Pin logs to the same Ready Pod UID/image before and after the run.
        if self.controller() != controller:
            raise ValueError("controller changed before receipt read")
        response = self.core.read_namespaced_pod_log(
            controller[0]["name"],
            "agent-platform",
            container="registry-controller",
            limit_bytes=1048576,
            _preload_content=False,
            _request_timeout=5,
        )
        try:
            raw = response.read(1048577)
            if len(raw) > 1048576:
                raise ValueError("controller log bound exceeded")
            lines = raw.decode("utf-8", errors="strict").splitlines()
        finally:
            response.close()
            response.release_conn()
        receipts = []
        for line in lines:
            try:
                row = json.loads(line[line.index("{") :])
            except (ValueError, TypeError):
                continue
            if (
                isinstance(row, dict)
                and row.get("event") == "tenant_network_policy_deleted"
                and row.get("name") == extra["name"]
            ):
                receipts.append(
                    {k: row.get(k) for k in ("event", "tenant", "name", "uid", "resource_version")}
                )
        return receipts


class PolicyRepairHarness:
    def __init__(self, client, ops, agent, release):
        self.client, self.ops, self.agent, self.release = client, ops, agent, release

    async def session_evidence(self, handle, task):
        result = await asyncio.wait_for(asyncio.shield(task), 90)
        events = [event async for event in handle.fetch_history_events()]
        scheduled = {
            e.event_id: e.activity_task_scheduled_event_attributes.activity_type.name
            for e in events
            if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
        }
        failures = []
        for event in events:
            if event.event_type != EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED:
                continue
            attrs = event.activity_task_failed_event_attributes
            failure = attrs.failure
            info = failure.application_failure_info
            if (
                scheduled.get(attrs.scheduled_event_id) != "preflight"
                or await failure_message(failure, self.client.data_converter) != MISMATCH
            ):
                continue
            details = await self.client.data_converter.decode(info.details.payloads)
            if len(details) == 1 and isinstance(details[0], dict):
                failures.append(
                    {
                        "activity": "preflight",
                        "type": info.type,
                        "non_retryable": bool(info.non_retryable),
                        "details": details[0],
                    }
                )
        return {
            "outcome": result.outcome,
            "activities": list(scheduled.values()),
            "failure": failures[0] if len(failures) == 1 else None,
            "pods_absent": not await asyncio.to_thread(self.ops.pods, handle.id.rsplit(":", 1)[-1]),
        }

    async def t28(self):
        sid = str(uuid.uuid4())
        data = {
            "tenant": self.ops.tenant,
            "session_id": sid,
            "cleanup": {"operator_deleted": False, "extra_absent": False, "session_closed": True},
        }
        extra = handle = task = None
        create_attempted = False
        policy_name = "t28-" + sid
        try:
            await asyncio.to_thread(self.ops.gate, self.agent, self.release)
            data["baseline"] = await asyncio.to_thread(self.ops.snapshot)
            if not data["baseline"]["valid"]:
                raise ValueError("unclean T28 baseline")
            data["controller_before"] = await asyncio.to_thread(self.ops.controller)
            began = time.monotonic()  # includes create API latency: conservative <=60s bound

            async def create_owned():
                nonlocal extra, create_attempted
                create_attempted = True
                extra = await asyncio.to_thread(self.ops.create, policy_name)
                data["extra"] = extra

            await settled(create_owned())
            data["create_elapsed"] = time.monotonic() - began
            if not extra["creation_confirmed"]:
                raise ValueError("ambiguous T28 create response")
            # Compute the expected drift digest from a real API read. If the controller
            # wins this race, retain a failed attempt; do not stop or delay it.
            drift = await asyncio.to_thread(self.ops.snapshot)
            data["drift_digest"] = drift["digest"]
            if drift["uids"].get(extra["name"]) != extra["uid"] or set(drift["names"]) != set(
                data["baseline"]["names"]
            ) | {extra["name"]}:
                raise ValueError(
                    "controller repaired before session submission or concurrent drift"
                )
            if time.monotonic() - began >= 60:
                raise TimeoutError("T28 create/submission window expired")
            inp = SessionInput(
                self.ops.tenant,
                self.agent,
                self.release,
                sid,
                None,
                {"goal": "T28 preflight refusal"},
                max_boundaries=1,
            )

            async def start_owned():
                nonlocal handle, task
                data["cleanup"]["session_closed"] = False
                try:
                    handle = await submit(self.client, inp)
                except Exception:
                    # A lost start response may already have created the exact
                    # workflow. Recover its deterministic handle for cancellation.
                    handle = self.client.get_workflow_handle(inp.workflow_id)
                    task = asyncio.create_task(handle.result())
                    raise
                task = asyncio.create_task(handle.result())

            await settled(start_owned())
            proof = asyncio.create_task(self.session_evidence(handle, task))
            try:
                while time.monotonic() - began <= 60:
                    snapshot = await asyncio.to_thread(self.ops.snapshot)
                    elapsed = time.monotonic() - began
                    if snapshot["valid"] and extra["name"] not in snapshot["uids"]:
                        data["repair"] = {**snapshot, "elapsed": elapsed, "extra_absent": True}
                        break
                    await asyncio.sleep(0.1)
                if "repair" not in data:
                    if proof.done():
                        data["session"] = await proof
                    raise TimeoutError("T28 controller repair deadline")
                data["session"] = await proof
            finally:
                if not proof.done():
                    proof.cancel()
                await asyncio.gather(proof, return_exceptions=True)
            data["receipts"] = await asyncio.to_thread(
                self.ops.receipts, data["controller_before"], extra
            )
            data["controller_after"] = await asyncio.to_thread(self.ops.controller)
        except Exception as exc:
            data["error_type"] = type(exc).__name__
        finally:

            async def cleanup():
                nonlocal extra
                if create_attempted and extra is None:
                    try:
                        extra = await asyncio.to_thread(self.ops.recover_owned, policy_name)
                        if extra is None:
                            data["cleanup"]["extra_absent"] = True
                        else:
                            data["extra"] = extra
                    except Exception as exc:
                        data["cleanup"]["recovery_error_type"] = type(exc).__name__
                if extra is not None:
                    try:
                        data["cleanup"]["operator_deleted"] = await asyncio.to_thread(
                            self.ops.remove_owned, extra
                        )
                        until = time.monotonic() + 10
                        while time.monotonic() < until:
                            snapshot = await asyncio.to_thread(self.ops.snapshot)
                            if extra["name"] not in snapshot["uids"]:
                                data["cleanup"]["extra_absent"] = True
                                break
                            await asyncio.sleep(0.1)
                    except Exception as exc:
                        data["cleanup"]["policy_error_type"] = type(exc).__name__
                # Cleanup is separate from timed repair evidence and never earns a pass.
                if handle is not None:
                    try:
                        if task is None or not task.done():
                            await handle.cancel()
                        if task is not None:
                            try:
                                await asyncio.wait_for(asyncio.shield(task), 60)
                            except WorkflowFailureError:
                                pass
                        data["cleanup"]["session_closed"] = True
                    except RPCError as exc:
                        if exc.status == RPCStatusCode.NOT_FOUND:
                            data["cleanup"]["session_closed"] = True
                            if task is not None:
                                await asyncio.gather(task, return_exceptions=True)
                        else:
                            data["cleanup"]["session_error_type"] = type(exc).__name__
                    except Exception as exc:
                        data["cleanup"]["session_error_type"] = type(exc).__name__
                try:
                    data["cleanup"]["session_pods_absent"] = not await asyncio.to_thread(
                        self.ops.pods, sid
                    )
                except Exception as exc:
                    data["cleanup"]["pod_error_type"] = type(exc).__name__

            await settled(cleanup())
        return evaluate(data)
