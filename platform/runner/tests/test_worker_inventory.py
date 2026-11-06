"""Candidate inventory binds exact selectors, installed image and owner UIDs."""

from copy import deepcopy
from types import SimpleNamespace as N
from unittest.mock import Mock

import pytest

from runner.worker_inventory import KubernetesInventory
from runner.worker_versioning import GuardError
from tests.test_worker_versioning import DEPLOYMENT, IMAGE


def setup():
    labels = {
        "app.kubernetes.io/name": "session-runner-governed",
        "agentplatform.io/tenant": "tenant-test",
        "agentplatform.io/worker-role": "sessions",
        "agentplatform.io/worker-pool": "governed-v1",
        "agentplatform.io/build-id": "new",
    }
    name = DEPLOYMENT + "-new"
    dep = N(
        metadata=N(
            name=name, uid="dep-uid", resource_version="10", generation=1, deletion_timestamp=None
        ),
        spec=N(replicas=1, selector=N(match_labels=labels, match_expressions=[])),
        status=N(observed_generation=1, available_replicas=1, updated_replicas=1, replicas=1),
    )
    rs = N(
        metadata=N(
            uid="rs-uid",
            deletion_timestamp=None,
            owner_references=[N(controller=True, kind="Deployment", name=name, uid="dep-uid")],
        )
    )
    pod = N(
        metadata=N(
            name="worker-new",
            uid="pod-uid",
            labels=labels,
            deletion_timestamp=None,
            owner_references=[N(controller=True, kind="ReplicaSet", name="rs", uid="rs-uid")],
        ),
        spec=N(
            containers=[
                N(
                    name="runner",
                    image=IMAGE,
                    env=[
                        N(name=k, value=v)
                        for k, v in {
                            "WORKER_POOL": "governed-v1",
                            "WORKER_BUILD_ID": "new",
                            "WORKER_DEPLOYMENT": DEPLOYMENT,
                        }.items()
                    ],
                )
            ]
        ),
        status=N(
            conditions=[N(type="Ready", status="True")],
            container_statuses=[
                N(name="runner", image_id=IMAGE, ready=True, restart_count=0, state=N(running=N()))
            ],
        ),
    )
    apps = N(
        read_namespaced_deployment=Mock(return_value=dep),
        read_namespaced_replica_set=Mock(return_value=rs),
    )
    core = N(list_namespaced_pod=Mock(return_value=N(items=[pod])))
    return KubernetesInventory(apps, core, tenant="tenant-test"), dep, rs, pod


@pytest.mark.asyncio
async def test_exact_candidate_owner_chain_and_image_are_retained():
    inventory, dep, rs, pod = setup()
    receipt = await inventory.candidate(DEPLOYMENT, "new", IMAGE)
    assert receipt["deployment_uid"] == dep.metadata.uid
    assert receipt["pods"][0]["uid"] == pod.metadata.uid
    assert receipt["pods"][0]["replicaset_uid"] == rs.metadata.uid


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken",
    [
        "selector",
        "generation",
        "rollout",
        "pod_deleted",
        "rs_replaced",
        "dep_replaced",
        "not_ready",
        "restart",
        "image",
        "image_id",
        "env",
        "extra_pod",
    ],
)
async def test_ambiguous_unready_or_replaced_candidate_is_refused(broken):
    inventory, dep, rs, pod = setup()
    if broken == "selector":
        dep.spec.selector.match_labels = {"app.kubernetes.io/name": "session-runner"}
    elif broken == "generation":
        dep.status.observed_generation = 0
    elif broken == "rollout":
        dep.status.updated_replicas = 0
    elif broken == "pod_deleted":
        pod.metadata.deletion_timestamp = "deleting"
    elif broken == "rs_replaced":
        rs.metadata.uid = "different"
    elif broken == "dep_replaced":
        rs.metadata.owner_references[0].uid = "different"
    elif broken == "not_ready":
        pod.status.conditions[0].status = "False"
    elif broken == "restart":
        pod.status.container_statuses[0].restart_count = 1
    elif broken == "image":
        pod.spec.containers[0].image = "unpinned"
    elif broken == "image_id":
        pod.status.container_statuses[0].image_id = "sha256:" + "b" * 64
    elif broken == "env":
        pod.spec.containers[0].env[0].value = "legacy"
    else:
        inventory.core.list_namespaced_pod.return_value.items.append(deepcopy(pod))
    with pytest.raises(GuardError):
        await inventory.candidate(DEPLOYMENT, "new", IMAGE)
