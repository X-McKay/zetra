"""netpol-attestor: the node DaemonSet of 5.5.

Every pass reads `iptables-save -t filter` once, then for each sandbox pod on this node
sets, refreshes (every 30 s) or removes `agentplatform.io/netpol-attested`.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import UTC, datetime

from netpol_attestor.parse import UNCHANGED, MatchConfig, Verdict, attest, decide

ANNOTATION = "agentplatform.io/netpol-attested"
SANDBOX_SELECTOR = "agentplatform.io/sandbox=true"
TENANT_PREFIX = "tenant-"
log = logging.getLogger("netpol-attestor")


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
            continue
        core.patch_namespaced_pod(name, ns, {"metadata": {"annotations": {ANNOTATION: value}}})
        if value is None:
            log.warning("attestation removed for %s/%s: %s", ns, name, verdict.reason)
        else:
            log.info("attested %s/%s chain=%s", ns, name, verdict.chain)


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
