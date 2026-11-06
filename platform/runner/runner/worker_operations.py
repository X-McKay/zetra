"""Read-only current-worker discovery, drain inspection and private history replay."""

from __future__ import annotations

from datetime import UTC, timedelta

from temporalio.api.deployment.v1 import WorkerDeploymentVersion
from temporalio.api.workflowservice import v1 as api
from temporalio.worker import Replayer

from runner.types import worker_deployment
from runner.worker_versioning import BUILD_RE, IMAGE_RE, GuardError, UNVERSIONED, sha256

RETENTION = timedelta(days=7)


class WorkerOperations:
    def __init__(self, operator):
        self.operator = operator

    async def current(self, role, pool, image):
        op = self.operator
        if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
            raise GuardError("current discovery requires a pinned runner image")
        deployment = worker_deployment(op.tenant, role, "governed-v1")
        if pool == "legacy":
            value = await op.legacy(deployment)
            if not value["workers"]["pods"] or any(
                p["image"] != image for p in value["workers"]["pods"]
            ):
                raise GuardError("legacy installed image differs from expected pin")
            return {"role": role, "pool": pool, "build_id": UNVERSIONED, **value}
        if pool != "governed-v1":
            raise GuardError("unknown worker pool")
        current, token = await op.routing(deployment)
        if current == UNVERSIONED:
            raise GuardError("governed pool has not been promoted")
        value = await op.prepare(deployment, current, image)
        after, after_token = await op.routing(deployment)
        if (
            after != current
            or token != after_token
            or value["current"] != current
            or value["conflict_sha256"] != sha256(token)
        ):
            raise GuardError("current worker routing changed during discovery")
        return {**value, "role": role, "pool": pool}

    async def version(self, deployment, build_id):
        op = self.operator
        queue = op.queue(deployment)
        if not isinstance(build_id, str) or not BUILD_RE.fullmatch(build_id):
            raise GuardError("invalid governed build")
        value = await op.call(
            "describe_worker_deployment_version",
            api.DescribeWorkerDeploymentVersionRequest(
                namespace=op.namespace,
                deployment_version=WorkerDeploymentVersion(
                    deployment_name=deployment, build_id=build_id
                ),
                report_task_queue_stats=True,
            ),
        )
        info = value.worker_deployment_version_info
        if info.deployment_version != WorkerDeploymentVersion(
            deployment_name=deployment, build_id=build_id
        ) or info.version not in ("", deployment + "." + build_id):
            raise GuardError("inspected version identity differs")
        queues = [(q.name, q.type) for q in info.task_queue_infos]
        if len(queues) != 2 or set(queues) != {(queue, 1), (queue, 2)}:
            raise GuardError("inspected version task queues differ")
        return info

    async def drain(self, deployment, build_id):
        op = self.operator
        current, token = await op.routing(deployment)
        info = await self.version(deployment, build_id)
        namespace = await op.call(
            "describe_namespace", api.DescribeNamespaceRequest(namespace=op.namespace)
        )
        if namespace.namespace_info.name != op.namespace:
            raise GuardError("namespace retention identity differs")
        retention = namespace.config.workflow_execution_retention_ttl.ToTimedelta()
        # Visibility supplements the server's drainage state. It can never replace
        # that state or establish submission closure/retention by being empty.
        executions = []
        page_token = b""
        for _ in range(10):
            page = await op.call(
                "list_workflow_executions",
                api.ListWorkflowExecutionsRequest(
                    namespace=op.namespace,
                    page_size=100,
                    next_page_token=page_token,
                    query=f'TemporalWorkerDeploymentVersion = "{deployment}:{build_id}"',
                ),
            )
            executions.extend(page.executions)
            page_token = bytes(page.next_page_token)
            if not page_token:
                break
        else:
            raise GuardError("version visibility exceeds bounded inspection; retain worker")
        closed = 0
        correlated = True
        for row in executions:
            actual = (
                await op.call(
                    "describe_workflow_execution",
                    api.DescribeWorkflowExecutionRequest(
                        namespace=op.namespace, execution=row.execution
                    ),
                )
            ).workflow_execution_info
            version = actual.versioning_info.deployment_version
            if version != WorkerDeploymentVersion(
                deployment_name=deployment, build_id=build_id
            ) or actual.task_queue != op.queue(deployment):
                correlated = False
            if actual.status in (2, 3, 4, 5, 6, 7) and actual.HasField("close_time"):
                closed += 1
        after, after_token = await op.routing(deployment)
        if (after, after_token) != (current, token):
            raise GuardError("routing changed during drain inspection")
        drainage = info.drainage_info

        def age(field):
            if not field.seconds and not field.nanos:
                return None
            delta = op.now() - field.ToDatetime(tzinfo=UTC)
            return delta.total_seconds() if delta.total_seconds() >= 0 else None

        inactive = age(info.last_deactivation_time)
        checked = age(drainage.last_checked_time)
        checks = {
            "not_current": current != build_id,
            "server_drained": drainage.status == 2,
            "server_drain_checked": checked is not None,
            "retention_config_at_least_seven_days": retention >= RETENTION,
            "visible_runs_correlated": correlated,
            "no_visible_open_runs": closed == len(executions),
            "seven_days_since_deactivation": inactive is not None
            and inactive >= RETENTION.total_seconds(),
            # One observation does not prove continuous replay/query availability.
            "seven_day_inactive_observation": False,
        }
        return {
            "schema": "worker-drain-inspection-v1",
            "deployment": deployment,
            "build_id": build_id,
            "current": current,
            "drainage_status": drainage.status,
            "observed_at": op.now().isoformat(),
            "deactivated_at": info.last_deactivation_time.ToJsonString()
            if inactive is not None
            else None,
            "last_checked_at": drainage.last_checked_time.ToJsonString()
            if checked is not None
            else None,
            "retention_seconds": retention.total_seconds(),
            "visible_runs": len(executions),
            "visible_closed_runs": closed,
            "checks": checks,
            "retirement_allowed": False,
            "limitation": "Read-only point observation; retain workers for pinned histories, replay and seven-day query evidence. No scale/delete operation is provided.",
        }

    async def history_receipt(self, client, deployment, build_id, image, workflow_id, run_id):
        op = self.operator
        prepared = await op.prepare(deployment, build_id, image)
        if client.namespace != op.namespace:
            raise GuardError("history client namespace differs")
        actual = (
            await op.call(
                "describe_workflow_execution",
                api.DescribeWorkflowExecutionRequest(
                    namespace=op.namespace, execution={"workflow_id": workflow_id, "run_id": run_id}
                ),
            )
        ).workflow_execution_info
        version = WorkerDeploymentVersion(deployment_name=deployment, build_id=build_id)
        if (
            actual.execution.workflow_id != workflow_id
            or actual.execution.run_id != run_id
            or actual.task_queue != op.queue(deployment)
            or actual.versioning_info.deployment_version != version
        ):
            raise GuardError("history receipt is not bound to exact run/queue/version")
        history = await client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history()
        raw = history.to_json().encode()
        if len(history.events) > 100000 or len(raw) > 64 << 20:
            raise GuardError("history exceeds bounded private inspection")
        pods = {p["name"] for p in prepared["workers"]["pods"]}

        def owned(identity):
            pid, sep, name = identity.partition("@")
            return bool(sep) and pid.isdigit() and name in pods

        workflows = []
        activities = []
        workflow_bound = True
        activity_bound = True
        for event in history.events:
            if event.HasField("workflow_task_completed_event_attributes"):
                value = event.workflow_task_completed_event_attributes
                workflow_bound = (
                    workflow_bound and value.deployment_version == version and owned(value.identity)
                )
                workflows.append(
                    {
                        "event_id": event.event_id,
                        "identity": value.identity if owned(value.identity) else None,
                        "deployment": deployment if value.deployment_version == version else None,
                        "build_id": build_id if value.deployment_version == version else None,
                    }
                )
            if event.HasField("activity_task_started_event_attributes"):
                value = event.activity_task_started_event_attributes
                activity_bound = (
                    activity_bound
                    and owned(value.identity)
                    and value.worker_version.build_id == build_id
                )
                activities.append(
                    {
                        "event_id": event.event_id,
                        "identity": value.identity if owned(value.identity) else None,
                        "build_id": build_id if value.worker_version.build_id == build_id else None,
                    }
                )
        after = await op.inventory.candidate(deployment, build_id, image)
        checks = {
            "workflow_tasks_bound": bool(workflows) and workflow_bound,
            "activity_starts_bound": bool(activities) and activity_bound,
            "same_worker_uids_and_images": after == prepared["workers"],
        }
        return {
            "schema": "worker-history-receipt-v1",
            "result": "pass" if all(checks.values()) else "fail",
            "deployment": deployment,
            "build_id": build_id,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "workflow_status": actual.status,
            "history_sha256": sha256(raw),
            "events": len(history.events),
            "workers": prepared["workers"],
            "workflow_tasks": workflows,
            "activity_starts": activities,
            "checks": checks,
        }

    async def replay(
        self,
        client,
        deployment,
        build_id,
        workflow_id,
        run_id,
        *,
        workflows=None,
        workflow_runner=None,
    ):
        op = self.operator
        await self.version(deployment, build_id)
        response = await op.call(
            "describe_workflow_execution",
            api.DescribeWorkflowExecutionRequest(
                namespace=op.namespace, execution={"workflow_id": workflow_id, "run_id": run_id}
            ),
        )
        info = response.workflow_execution_info
        if (
            info.execution.workflow_id != workflow_id
            or info.execution.run_id != run_id
            or info.versioning_info.deployment_version
            != WorkerDeploymentVersion(deployment_name=deployment, build_id=build_id)
            or info.task_queue != op.queue(deployment)
        ):
            raise GuardError("replay history is not bound to exact queue/version/run")
        if info.status not in (2, 3, 4, 5, 6, 7) or not info.HasField("close_time"):
            raise GuardError("replay requires a closed exact history")
        if client.namespace != op.namespace:
            raise GuardError("replay client namespace differs")
        if workflows is None:
            from runner.workflow import SessionWorkflow, CleanupReconcilerWorkflow

            workflows = [SessionWorkflow, CleanupReconcilerWorkflow]
        history = await client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history()
        raw = history.to_json().encode()
        if len(history.events) > 100000 or len(raw) > 64 << 20:
            raise GuardError("history exceeds bounded private replay")
        options = {"workflows": workflows, "data_converter": client.data_converter}
        if workflow_runner is not None:
            options["workflow_runner"] = workflow_runner
        await Replayer(**options).replay_workflow(history)
        import inspect

        sources = {w.__name__: sha256(inspect.getsource(w).encode()) for w in workflows}
        return {
            "schema": "worker-replay-v1",
            "deployment": deployment,
            "build_id": build_id,
            "workflow_id": workflow_id,
            "run_id": run_id,
            "history_sha256": sha256(raw),
            "events": len(history.events),
            "replayed": True,
            "workflow_sources_sha256": sources,
            "scope": "Replay against this installed operator code; raw history remains private and is never written or printed.",
        }
