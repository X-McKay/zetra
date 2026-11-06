"""netpol-attestor: the node DaemonSet of 5.5.

Every pass reads `iptables-save -t filter` once, then for each sandbox pod on this node
sets, refreshes (every 30 s) or removes `agentplatform.io/netpol-attested`.
"""

from __future__ import annotations

import ipaddress
import itertools
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime

from kubernetes.client.exceptions import ApiException

from netpol_attestor.parse import UNCHANGED, MatchConfig, Verdict, attest, decide

ANNOTATION = "agentplatform.io/netpol-attested"
SANDBOX_SELECTOR = "agentplatform.io/sandbox=true"
TENANT_PREFIX = "tenant-"
log = logging.getLogger("netpol-attestor")
PROCESS_ID = str(uuid.uuid4())
SEQUENCE = itertools.count(1)


def receipt(event: str, **fields) -> None:
    """Only explicit platform metadata enters the trusted receipt stream."""
    log.info(
        json.dumps(
            {
                "schema": "netpol-attestor-v1",
                "event": event,
                "process_id": PROCESS_ID,
                "sequence": next(SEQUENCE),
                "observed_ns": time.time_ns(),
                "monotonic_ns": time.monotonic_ns(),
                **fields,
            },
            sort_keys=True,
        )
    )


def binding(pod, node: str) -> dict:
    """Reject missing identity, and never log arbitrary metadata or annotations."""
    meta = pod.metadata
    uid, rv = getattr(meta, "uid", None), getattr(meta, "resource_version", None)
    if not isinstance(uid, str) or str(uuid.UUID(uid)) != uid:
        raise ValueError("missing canonical Pod UID")
    if not isinstance(rv, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", rv):
        raise ValueError("missing Pod resource version")
    for value in (meta.namespace, meta.name, node):
        if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", value):
            raise ValueError("invalid Pod/node identity")
    return {
        "namespace": meta.namespace,
        "pod": meta.name,
        "pod_uid": uid,
        "pod_ip": str(ipaddress.ip_address(pod.status.pod_ip)) if pod.status.pod_ip else None,
        "node": node,
        "source_resource_version": rv,
    }


def annotation_fields(value: str | None) -> dict:
    # Configurable chain names retain their existing operational behavior. Only
    # the pinned kube-router format is eligible for exported startup evidence.
    safe = isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ;KUBE-POD-FW-[A-Z0-9]{1,32}", value
    )
    return {
        "annotation_recorded": value is None or bool(safe),
        "annotation": value if safe else None,
        "chain": value.split(";", 1)[1] if safe else None,
    }


def conditional_patch(pod, value: str | None) -> list[dict]:
    meta = pod.metadata
    ops = [
        {"op": "test", "path": "/metadata/uid", "value": meta.uid},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta.resource_version},
    ]
    path = "/metadata/annotations/" + ANNOTATION.replace("~", "~0").replace("/", "~1")
    if value is None:
        ops.append({"op": "remove", "path": path})
    elif not meta.annotations:
        ops.append({"op": "add", "path": "/metadata/annotations", "value": {ANNOTATION: value}})
    else:
        ops.append({"op": "add", "path": path, "value": value})
    return ops


def read_filter_table(command: str) -> str:
    return subprocess.run(
        [command, "-t", "filter"], capture_output=True, text=True, check=True, timeout=20
    ).stdout


def run_pass(core, node: str, iptables: str, cfg: MatchConfig, refresh: float) -> None:
    pods = core.list_pod_for_all_namespaces(
        field_selector=f"spec.nodeName={node}", label_selector=SANDBOX_SELECTOR
    ).items
    if not pods:
        return
    try:
        table = read_filter_table(iptables)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("cannot read the filter table: %s", type(exc).__name__)
        table = None  # every attestation is withdrawn: fail closed
    now = datetime.now(UTC)
    for pod in pods:
        ns, name = pod.metadata.namespace, pod.metadata.name
        if not ns.startswith(TENANT_PREFIX):
            continue
        try:
            identity = binding(pod, node)
        except (AttributeError, TypeError, ValueError):
            receipt("pod_binding_invalid")
            continue
        current = (pod.metadata.annotations or {}).get(ANNOTATION)
        ip = pod.status.pod_ip
        if pod.metadata.deletion_timestamp or pod.status.phase in ("Succeeded", "Failed"):
            verdict = Verdict(None, "pod_finished")
        elif not ip:
            continue
        elif table is None:
            verdict = Verdict(None, "table_unreadable")
        else:
            verdict = attest(table, ip, cfg)
        value = decide(current, verdict, now, refresh)
        if value is UNCHANGED:
            if not verdict.ok:
                receipt("chain_not_verified", reason=verdict.reason, **identity)
            continue
        evidence = {
            **identity,
            **annotation_fields(value),
            "operation_id": str(uuid.uuid4()),
            "reason": verdict.reason,
        }
        receipt("chain_verified" if value is not None else "attestation_withdrawal", **evidence)
        try:
            # List bodies use application/json-patch+json in the pinned client.
            # Both tests are atomic with the write. Never reuse this verdict after
            # a conflict: the next pass reads the current Pod and host table anew.
            result = core.patch_namespaced_pod(
                name, ns, conditional_patch(pod, value), _request_timeout=10
            )
        except ApiException as exc:
            receipt("annotation_patch_failed", status=exc.status, failure="api", **evidence)
            continue
        try:
            response = binding(result, node)
            annotations = result.metadata.annotations or {}
            matching = (
                all(response[k] == identity[k] for k in ("namespace", "pod", "pod_uid", "pod_ip"))
                and annotations.get(ANNOTATION) == value
                and (value is not None or ANNOTATION not in annotations)
            )
        except (AttributeError, TypeError, ValueError):
            matching = False
        if not matching:
            receipt("annotation_patch_failed", failure="response_mismatch", **evidence)
            continue
        receipt(
            "annotation_patch_ack", resource_version=result.metadata.resource_version, **evidence
        )


def main() -> int:
    from kubernetes import client, config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config.load_incluster_config()
    core = client.CoreV1Api()
    node = os.environ["NODE_NAME"]
    iptables = os.environ.get("ATTESTOR_IPTABLES_SAVE", "iptables-save")
    poll = float(os.environ.get("ATTESTOR_POLL_SECONDS", "2"))
    refresh = float(os.environ.get("ATTESTOR_REFRESH_SECONDS", "30"))
    cfg = MatchConfig.from_env()
    log.info("watching sandbox pods on %s with %s", node, cfg)
    while True:
        try:
            run_pass(core, node, iptables, cfg, refresh)
        except Exception as exc:  # keep running; the APS ages out stale attestations
            log.error("pass failed: %s", type(exc).__name__)
        time.sleep(poll)


if __name__ == "__main__":
    sys.exit(main())
