"""CAS changes to future cleanup Schedule actions, preserving encrypted payloads."""

from __future__ import annotations

import copy
import uuid

from temporalio.api.workflowservice import v1 as api

from runner.types import worker_deployment, worker_queue
from runner.worker_versioning import GuardError, RPC_TIMEOUT, sha256


class CleanupScheduleOperator:
    def __init__(self, service, versions):
        self.service, self.versions = service, versions
        self.tenant = versions.tenant
        self.namespace = f"agents-{self.tenant}"
        self.schedule_id = f"cleanup-reconciler-{self.tenant}"

    async def describe(self):
        value = await self.service.describe_schedule(
            api.DescribeScheduleRequest(
                namespace=self.namespace,
                schedule_id=self.schedule_id,
            ),
            timeout=RPC_TIMEOUT,
        )
        schedule = value.schedule
        # Temporal1.29.1 reads an eight-byte big-endian counter; shorter
        # tokens become zero, and zero explicitly disables conflict checking.
        if len(value.conflict_token) != 8 or int.from_bytes(value.conflict_token, "big") == 0:
            raise GuardError("Schedule conflict token must be eight nonzero-counter bytes")
        spec = schedule.spec
        if (
            len(spec.interval) != 1
            or spec.interval[0].interval.seconds != 300
            or spec.interval[0].interval.nanos
            or spec.interval[0].phase.seconds
            or spec.interval[0].phase.nanos
        ):
            raise GuardError("Schedule must retain exact five-minute interval")
        # Refuse a shape with extra calendar/cron/bounds/jitter instead of
        # silently accepting changed trigger semantics.
        expected_spec = type(spec)(interval=[spec.interval[0]])
        if spec != expected_spec or schedule.policies.overlap_policy != 1:
            raise GuardError("Schedule must retain interval-only SKIP policy")
        action = schedule.action
        if not action.HasField("start_workflow"):
            raise GuardError("Schedule lacks cleanup Workflow action")
        start = action.start_workflow
        if (
            start.workflow_type.name != "CleanupReconcilerWorkflow"
            or start.workflow_id != f"cleanup-reconciler:{self.tenant}"
            or start.versioning_override.ByteSize()
        ):
            raise GuardError("Schedule Workflow identity or version override differs")
        if start.task_queue.name not in {
            worker_queue(self.tenant, "cleanup", pool) for pool in ("legacy", "governed-v1")
        }:
            raise GuardError("Schedule queue outside exact cleanup pools")
        return value

    @staticmethod
    def fingerprint(value):
        # Include opaque payload and Schedule metadata without exposing it.
        clone = type(value)()
        clone.CopyFrom(value)
        clone.ClearField("info")  # runtime counters/times change without an edit
        clone.ClearField("conflict_token")
        return sha256(clone.SerializeToString(deterministic=True))

    async def inspect(self):
        value = await self.describe()
        return {
            "expected_queue": value.schedule.action.start_workflow.task_queue.name,
            "expected_conflict_sha256": sha256(value.conflict_token),
            "expected_schedule_sha256": self.fingerprint(value),
        }

    async def route(
        self,
        pool,
        *,
        expected_queue,
        expected_conflict_sha256,
        expected_schedule_sha256,
        build_id=None,
        image=None,
    ):
        target = worker_queue(self.tenant, "cleanup", pool)
        if target == expected_queue:
            raise GuardError("Schedule already targets requested pool")
        deployment = worker_deployment(self.tenant, "cleanup", "governed-v1")
        if pool == "governed-v1":
            ready = await self.versions.prepare(deployment, build_id, image)
            if ready["current"] != build_id:
                raise GuardError("Schedule target is not the current governed build")
        else:
            await self.versions.legacy(deployment)
        value = await self.describe()
        if (
            value.schedule.action.start_workflow.task_queue.name != expected_queue
            or sha256(value.conflict_token) != expected_conflict_sha256
            or self.fingerprint(value) != expected_schedule_sha256
        ):
            raise GuardError("Schedule changed since preparation")
        updated = type(value.schedule)()
        updated.CopyFrom(value.schedule)
        updated.action.start_workflow.task_queue.name = target
        pending = {
            "schema": "cleanup-schedule-routing-v1",
            "schedule_id": self.schedule_id,
            "previous_queue": expected_queue,
            "target_queue": target,
            "status": "mutation_pending",
            "conflict_sha256": expected_conflict_sha256,
            "schedule_sha256": expected_schedule_sha256,
        }
        self.versions.record(copy.deepcopy(pending))
        try:
            await self.service.update_schedule(
                api.UpdateScheduleRequest(
                    namespace=self.namespace,
                    schedule_id=self.schedule_id,
                    schedule=updated,
                    conflict_token=value.conflict_token,
                    memo=value.memo,
                    search_attributes=value.search_attributes,
                    identity="agent-platform-worker-versioning",
                    request_id=str(uuid.uuid4()),
                ),
                timeout=RPC_TIMEOUT,
            )
            after = await self.describe()
            if (
                after.schedule != updated
                or after.memo != value.memo
                or after.search_attributes != value.search_attributes
            ):
                raise GuardError("Schedule readback differs from exact update")
        except Exception as exc:
            result = {**pending, "status": "uncertain", "error_type": type(exc).__name__}
            try:
                result["observed_queue"] = (
                    await self.describe()
                ).schedule.action.start_workflow.task_queue.name
            except Exception as read_error:
                result["readback_error_type"] = type(read_error).__name__
            self.versions.record(copy.deepcopy(result))
            return result
        result = {**pending, "status": "confirmed"}
        self.versions.record(copy.deepcopy(result))
        return result
