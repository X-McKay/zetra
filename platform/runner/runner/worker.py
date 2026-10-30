"""Session Runner worker (10.6) and the cleanup reconciler worker and Schedule (10.7)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    SchedulePolicy,
    ScheduleOverlapPolicy,
    ScheduleSpec,
    TLSConfig,
)
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

from runner import activities as act
from runner.codec import AesGcmCodec, data_converter
from runner.types import TENANT_RE
from runner.workflow import CleanupReconcilerWorkflow, SessionWorkflow

RECONCILE_EVERY = timedelta(minutes=5)


def session_queue(tenant: str) -> str:
    return f"sessions-{tenant}"


def cleanup_queue(tenant: str) -> str:
    return f"cleanup-{tenant}"


async def connect(tenant: str) -> Client:
    """mTLS to the frontend, namespace agents-<tenant>, per-tenant AES-GCM codec."""
    if not TENANT_RE.match(tenant):
        raise SystemExit("TENANT must match ^tenant-[a-z0-9-]{1,50}$")
    env = os.environ
    tls: TLSConfig | bool = False
    if env.get("TEMPORAL_TLS_CERT"):
        tls = TLSConfig(
            client_cert=Path(env["TEMPORAL_TLS_CERT"]).read_bytes(),
            client_private_key=Path(env["TEMPORAL_TLS_KEY"]).read_bytes(),
            server_root_ca_cert=Path(env["TEMPORAL_TLS_CA"]).read_bytes()
            if env.get("TEMPORAL_TLS_CA")
            else None,
            domain=env.get("TEMPORAL_TLS_SERVER_NAME") or None,
        )
    codec = AesGcmCodec.from_file(
        env.get("PAYLOAD_KEY_FILE", f"/etc/agent-platform/payload-key/{tenant}"), tenant
    )
    return await Client.connect(
        env.get("TEMPORAL_ADDRESS", "temporal-frontend.temporal.svc.cluster.local:7233"),
        namespace=env.get("TEMPORAL_NAMESPACE", f"agents-{tenant}"),
        tls=tls,
        data_converter=data_converter(codec),
    )


def _deployment() -> WorkerDeploymentConfig | None:
    build_id = os.environ.get("WORKER_BUILD_ID")
    if not build_id:
        return None
    # Pinned: an in-flight history replays only on the build that started it (14.4).
    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(
            os.environ.get("WORKER_DEPLOYMENT", "session-runner"), build_id
        ),
        use_worker_versioning=True,
        default_versioning_behavior=VersioningBehavior.PINNED,
    )


def session_worker(client: Client, tenant: str, executor: ThreadPoolExecutor) -> Worker:
    return Worker(
        client,
        task_queue=session_queue(tenant),
        workflows=[SessionWorkflow],
        activities=act.SESSION_ACTIVITIES,
        activity_executor=executor,
        max_concurrent_activities=16,
        deployment_config=_deployment(),
    )


def reconciler_worker(client: Client, tenant: str, executor: ThreadPoolExecutor) -> Worker:
    return Worker(
        client,
        task_queue=cleanup_queue(tenant),
        workflows=[CleanupReconcilerWorkflow],
        activities=act.RECONCILER_ACTIVITIES,
        activity_executor=executor,
        max_concurrent_activities=16,
        deployment_config=_deployment(),
    )


async def ensure_reconciler_schedule(client: Client, tenant: str) -> None:
    try:
        await client.create_schedule(
            f"cleanup-reconciler-{tenant}",
            Schedule(
                action=ScheduleActionStartWorkflow(
                    CleanupReconcilerWorkflow.run,
                    tenant,
                    id=f"cleanup-reconciler:{tenant}",
                    task_queue=cleanup_queue(tenant),
                    execution_timeout=timedelta(minutes=15),
                ),
                spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=RECONCILE_EVERY)]),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
            ),
        )
    except ScheduleAlreadyRunningError:
        pass


async def run(role: str, tenant: str) -> None:
    client = await connect(tenant)
    deps = act.Deps.from_cluster()
    deps.temporal = client
    act.configure(deps)
    with ThreadPoolExecutor(max_workers=16) as executor:
        if role == "reconciler":
            await ensure_reconciler_schedule(client, tenant)
            worker = reconciler_worker(client, tenant, executor)
        else:
            worker = session_worker(client, tenant, executor)
        await worker.run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent platform Session Runner worker")
    parser.add_argument("--role", choices=["sessions", "reconciler"], default="sessions")
    parser.add_argument("--tenant", default=os.environ.get("TENANT", ""))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run(args.role, args.tenant))


if __name__ == "__main__":
    main()
