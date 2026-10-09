"""Qualify the lab OCI worker with a real Kubernetes Temporal workflow and replay.

The caller creates the dedicated qualification fixture first. This client only
targets kind-zetra-validation, uses loopback port-forwarding, and cleans up its
own port-forward. It never enables the native production release renderer.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Replayer

from zetra.temporal_workflow import AgentWorkflow


async def qualify(args) -> dict:
    if args.context != "kind-zetra-validation":
        raise ValueError("This lab fixture only targets kind-zetra-validation")
    kube = [args.kubectl, "--kubeconfig", args.kubeconfig, "--context", args.context]
    namespace = "zetra-worker-qualification"
    pod = "zetra-worker-temporal"
    state = Path(".tools")
    state.mkdir(exist_ok=True)
    log_path = state / "worker-kubernetes-port-forward.log"
    with log_path.open("wb") as log:
        forward = subprocess.Popen(
            [
                *kube,
                "-n",
                namespace,
                "port-forward",
                f"pod/{pod}",
                "17233:7233",
                "--address",
                "127.0.0.1",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            client = None
            last_error = "Port-forward did not become ready"
            for _ in range(30):
                if forward.poll() is not None:
                    raise RuntimeError(log_path.read_text())
                try:
                    client = await Client.connect("127.0.0.1:17233", namespace="zetra-worker-lab")
                    break
                except (RuntimeError, OSError) as exc:
                    last_error = str(exc)
                    await asyncio.sleep(1)
            if client is None:
                raise RuntimeError(last_error)
            workflow_id = f"zetra-oci-kubernetes-{uuid.uuid4()}"
            result = await asyncio.wait_for(
                client.execute_workflow(
                    AgentWorkflow.run,
                    {"key": "zetra"},
                    id=workflow_id,
                    task_queue="zetra-image-qualification",
                    execution_timeout=timedelta(seconds=60),
                ),
                timeout=65,
            )
            expected = {
                "answer": "Zetra means ZEro-TRust Agents.",
                "source": "approved-knowledge-fixture",
            }
            if result != expected:
                raise AssertionError(f"Unexpected controlled fixture result: {result!r}")
            history = await client.get_workflow_handle(workflow_id).fetch_history()
            history_json = history.to_json()
            (state / "worker-kubernetes-history.json").write_text(history_json)
            await Replayer(workflows=[AgentWorkflow]).replay_workflow(history)
            snapshot = subprocess.run(
                [*kube, "-n", namespace, "get", "pod", pod, "-o", "json"],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            pod_state = json.loads(snapshot.stdout)
            status = pod_state["status"]
            worker = next(c for c in pod_state["spec"]["containers"] if c["name"] == "worker")
            worker_status = next(c for c in status["containerStatuses"] if c["name"] == "worker")
            container_id = worker_status["containerID"].removeprefix("containerd://")
            if re.fullmatch(r"[a-f0-9]{64}", container_id) is None:
                raise ValueError("Unexpected worker container ID")
            node = [
                args.podman,
                "--connection",
                "zetra-validation-root",
                "exec",
                "zetra-validation-control-plane",
            ]

            def node_output(*command: str) -> str:
                return subprocess.run(
                    [*node, *command],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=20,
                ).stdout

            live_container = json.loads(node_output("crictl", "inspect", container_id))
            live_metadata = json.loads(
                node_output("ctr", "-n", "k8s.io", "containers", "info", container_id)
            )
            image_info = json.loads(node_output("crictl", "inspecti", worker["image"]))
            index_digest = worker["image"].split("@", 1)[1]
            index_json = node_output("ctr", "-n", "k8s.io", "content", "get", index_digest)
            index = json.loads(index_json)
            if "sha256:" + hashlib.sha256(index_json.encode()).hexdigest() != index_digest:
                raise AssertionError("Index name does not match its actual content digest")
            if len(index["manifests"]) != 1:
                raise ValueError("This ARM64 lab expects a single-platform OCI index")
            manifest_digest = index["manifests"][0]["digest"]
            manifest_json = node_output("ctr", "-n", "k8s.io", "content", "get", manifest_digest)
            if "sha256:" + hashlib.sha256(manifest_json.encode()).hexdigest() != manifest_digest:
                raise AssertionError("Manifest name does not match its actual content digest")
            image_build = json.loads(Path("integrations/evidence/worker-image.json").read_text())
            expected_config = image_build["imageId"]
            if (
                json.loads(manifest_json)["config"]["digest"] != expected_config
                or image_info["status"]["id"] != expected_config
                or live_container["status"]["imageRef"].rsplit("@", 1)[-1] != index_digest
            ):
                raise AssertionError("Live worker image does not correlate to the final build")
            metadata_target = node_output(
                "ctr", "-n", "k8s.io", "images", "list", "name==" + live_metadata["Image"]
            ).splitlines()
            if len(metadata_target) != 2 or metadata_target[1].split()[2] not in {
                index_digest,
                manifest_digest,
            }:
                raise AssertionError("Live containerd alias target is outside the verified chain")
            return {
                "checkedAt": datetime.now(UTC).isoformat(),
                "status": "passed",
                "scope": "Actual OCI worker in disposable Kubernetes pod with loopback Temporal development server",
                "context": args.context,
                "kubernetesNamespace": namespace,
                "pod": pod,
                "workerImage": worker["image"],
                "temporal": {
                    "cli": "1.9.1",
                    "server": "1.32.0",
                    "namespace": "zetra-worker-lab",
                    "connection": "Pod-local loopback plaintext; host client uses loopback-only port-forward",
                },
                "workflow": {
                    "id": workflow_id,
                    "taskQueue": "zetra-image-qualification",
                    "result": result,
                    "events": len(history.events),
                    "historySHA256": hashlib.sha256(history_json.encode()).hexdigest(),
                    "replay": "passed using temporalio 1.34.0 Replayer and actual recorded history",
                },
                "podSecurityContext": pod_state["spec"]["securityContext"],
                "workerSecurityContext": worker["securityContext"],
                "automountServiceAccountToken": pod_state["spec"]["automountServiceAccountToken"],
                "containerStatuses": status.get("containerStatuses", []),
                "imageCorrelation": {
                    "capturedBeforeCleanup": True,
                    "liveContainerID": container_id,
                    "liveContainerdImageReference": live_metadata["Image"],
                    "liveContainerdAliasTargetDigest": metadata_target[1].split()[2],
                    "liveCRIImageReference": live_container["status"]["imageRef"],
                    "liveCRIConfigImageID": image_info["status"]["id"],
                    "indexDigest": index_digest,
                    "platformManifestDigest": manifest_digest,
                    "configImageID": expected_config,
                    "indexContentHashVerified": True,
                    "manifestContentHashVerified": True,
                    "liveConfigMatchesFinalBuild": True,
                    "reasonForDifferentDigestForms": "CRI deduplicates image config IDs and preserves its preferred tag/import name in live container metadata. The live CRI imageRef digest equals the retained OCI index; its descriptor selects the ARM64 manifest, which references the final config. The containerd alias target and each content hash were checked independently.",
                },
                "limitations": [
                    "Single pod development server with ephemeral SQLite; not production HA, persistence or mTLS qualification",
                    "Controlled public-data knowledge fixture; no live model or external tool execution",
                    "NetworkPolicy is configured; this workflow does not independently negative-test network enforcement",
                    "This separate fixture does not change the native renderer's withheld replica count or production authorization",
                    "History replay covers this execution, not all production histories or cross-version migrations",
                ],
                "cleanup": {
                    "portForward": "terminated",
                    "namespace": "caller-owned; cleanup pending",
                },
            }
        finally:
            forward.terminate()
            try:
                forward.wait(timeout=5)
            except subprocess.TimeoutExpired:
                forward.kill()
                forward.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--podman", default="podman")
    parser.add_argument(
        "--output", default="integrations/evidence/worker-temporal-kubernetes-report.json"
    )
    args = parser.parse_args()
    report = asyncio.run(qualify(args))
    Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "workflow": report["workflow"]}, indent=2))


if __name__ == "__main__":
    main()
