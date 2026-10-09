"""Real scoped eBPF and CNI fixture qualification; disposable kind context only."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command(prefix: list[str], *args: str, timeout: int = 40):
    return subprocess.run([*prefix, *args], capture_output=True, text=True, timeout=timeout)


def checked(prefix: list[str], *args: str):
    result = command(prefix, *args)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args)}: {result.stderr[:1200]}")
    return result.stdout


def file_hash(name: str) -> str:
    return hashlib.sha256((ROOT / "integrations" / name).read_bytes()).hexdigest()


def qualify(kubeconfig: str, output: Path) -> None:
    prefix = [
        "kubectl",
        "--kubeconfig",
        str(Path(kubeconfig).resolve()),
        "--context",
        "kind-zetra-validation",
        "--request-timeout=10s",
    ]
    checks = []

    def record(name: str, passed: bool, detail: dict) -> None:
        checks.append({"id": name, "status": "passed" if passed else "failed", **detail})

    for pod in ["tetragon-guarded", "tetragon-unselected"]:
        checked(
            prefix,
            "delete",
            "pod",
            pod,
            "-n",
            "zetra-validation",
            "--ignore-not-found=true",
            "--wait=true",
        )
    checked(prefix, "apply", "-f", str(ROOT / "integrations/tetragon-fixture.yaml"))
    checked(
        prefix,
        "wait",
        "--for=condition=Ready",
        "pod/tetragon-guarded",
        "pod/tetragon-unselected",
        "-n",
        "zetra-validation",
        "--timeout=30s",
    )
    checked(prefix, "apply", "-f", str(ROOT / "integrations/tetragon-policy.yaml"))
    listing = checked(
        prefix,
        "exec",
        "-n",
        "kube-system",
        "ds/tetragon",
        "-c",
        "tetragon",
        "--",
        "tetra",
        "tracingpolicy",
        "list",
    )
    record(
        "ebpf-policy-loaded",
        "zetra-fixture-file-guard" in listing and "enabled" in listing,
        {"loadedPolicyList": listing.strip()},
    )
    # The pods are already running: this deliberately does NOT test startup-gap closure.
    time.sleep(2)
    allowed = command(
        prefix, "exec", "-n", "zetra-validation", "tetragon-guarded", "--", "cat", "/etc/hostname"
    )
    unselected = command(
        prefix,
        "exec",
        "-n",
        "zetra-validation",
        "tetragon-unselected",
        "--",
        "cat",
        "/tmp/zetra-sensitive",
    )
    denied = command(
        prefix,
        "exec",
        "-n",
        "zetra-validation",
        "tetragon-guarded",
        "--",
        "cat",
        "/tmp/zetra-sensitive",
    )
    record("ebpf-unrelated-read-allowed", allowed.returncode == 0, {"exitCode": allowed.returncode})
    record(
        "ebpf-unselected-pod-unaffected",
        unselected.returncode == 0 and unselected.stdout == "synthetic-test-data",
        {"exitCode": unselected.returncode},
    )
    record(
        "ebpf-selected-sensitive-read-killed",
        denied.returncode == 137 and denied.stdout == "",
        {"exitCode": denied.returncode, "outputBytes": len(denied.stdout.encode())},
    )
    time.sleep(1)
    log = checked(
        prefix, "logs", "-n", "kube-system", "ds/tetragon", "-c", "export-stdout", "--since=2m"
    )
    selected = []
    normalized = []
    for line in log.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        for kind, payload in event.items():
            if not isinstance(payload, dict):
                continue
            process = payload.get("process", {})
            pod = process.get("pod", {})
            if (
                pod.get("namespace") != "zetra-validation"
                or pod.get("name") != "tetragon-guarded"
                or pod.get("pod_labels", {}).get("zetra.dev/agent") != "tetragon-fixture"
            ):
                continue
            container = pod.get("container", {})
            if container.get("name") != "agent":
                continue
            base = {
                "agent": "tetragon-fixture",
                "image": container.get("image", {}).get("id"),
                "container": "agent",
                "podUid": pod.get("uid"),
                "execId": process.get("exec_id"),
            }
            if (
                kind == "process_kprobe"
                and payload.get("policy_name") == "zetra-fixture-file-guard"
            ):
                path = payload.get("args", [{}])[0].get("file_arg", {}).get("path")
                selected.append(
                    {
                        "kind": kind,
                        "podUid": pod.get("uid"),
                        "image": base["image"],
                        "binary": process.get("binary"),
                        "path": path,
                        "action": payload.get("action"),
                        "policy": payload.get("policy_name"),
                    }
                )
                normalized.append(
                    {
                        **base,
                        "kind": "file",
                        "path": path,
                        "mode": "read",
                        "outcome": "denied"
                        if payload.get("action") == "KPROBE_ACTION_SIGKILL"
                        else "observed",
                    }
                )
            elif kind == "process_exec" and process.get("binary") == "/bin/cat":
                normalized.append({**base, "kind": "exec", "binary": process["binary"]})
    record(
        "ebpf-event-identity-action-correlated",
        any(e.get("action") == "KPROBE_ACTION_SIGKILL" for e in selected),
        {"events": selected},
    )
    checked(
        prefix,
        "delete",
        "pod",
        "client",
        "approved-server",
        "forbidden-server",
        "-n",
        "zetra-network-test",
        "--ignore-not-found=true",
        "--grace-period=1",
    )
    checked(prefix, "apply", "-f", str(ROOT / "integrations/network-fixture.yaml"))
    checked(
        prefix,
        "wait",
        "--for=condition=Ready",
        "pod/client",
        "pod/approved-server",
        "pod/forbidden-server",
        "-n",
        "zetra-network-test",
        "--timeout=30s",
    )
    # A rerun removes only this fixture policy to establish baseline reachability.
    checked(
        prefix,
        "delete",
        "networkpolicy",
        "zetra-fixture-gateway-only",
        "-n",
        "zetra-network-test",
        "--ignore-not-found=true",
    )
    pods = json.loads(checked(prefix, "get", "pods", "-n", "zetra-network-test", "-o", "json"))
    ips = {p["metadata"]["name"]: p["status"]["podIP"] for p in pods["items"]}

    def get(server: str):
        return command(
            prefix,
            "exec",
            "-n",
            "zetra-network-test",
            "client",
            "--",
            "wget",
            "-T",
            "3",
            "-qO-",
            f"http://{ips[server]}:8080",
        )

    baseline = get("forbidden-server")
    baseline_attempts = 1
    # CNI policy deletion/endpoint programming is asynchronous. Establish a
    # bounded reachable baseline before using denial as enforcement evidence.
    while baseline.returncode and baseline_attempts < 4:
        time.sleep(1)
        baseline = get("forbidden-server")
        baseline_attempts += 1
    record(
        "network-baseline-target-reachable",
        baseline.returncode == 0 and baseline.stdout == "forbidden-fixture",
        {
            "exitCode": baseline.returncode,
            "attempts": baseline_attempts,
            "error": baseline.stderr.strip(),
        },
    )
    checked(prefix, "apply", "-f", str(ROOT / "integrations/network-policy.yaml"))
    time.sleep(2)
    approved, forbidden = get("approved-server"), get("forbidden-server")
    record(
        "network-approved-peer-allowed",
        approved.returncode == 0 and approved.stdout == "approved-fixture",
        {"exitCode": approved.returncode},
    )
    record(
        "network-forbidden-direct-peer-denied",
        forbidden.returncode != 0 and not forbidden.stdout,
        {"exitCode": forbidden.returncode, "outputBytes": len(forbidden.stdout.encode())},
    )
    nodes = json.loads(checked(prefix, "get", "nodes", "-o", "json"))
    report = {
        "apiVersion": "zetra.dev/live-boundary-report-v1alpha1",
        "checkedAt": datetime.now(UTC).isoformat(),
        "status": "passed" if all(c["status"] == "passed" for c in checks) else "failed",
        "context": "kind-zetra-validation",
        "nodes": [
            {
                "name": n["metadata"]["name"],
                "kernel": n["status"]["nodeInfo"]["kernelVersion"],
                "runtime": n["status"]["nodeInfo"]["containerRuntimeVersion"],
                "architecture": n["status"]["nodeInfo"]["architecture"],
            }
            for n in nodes["items"]
        ],
        "versions": {"tetragon": "1.7.1", "cilium": "1.20.2"},
        "checks": checks,
        "fixtureHashes": {
            n: file_hash(n)
            for n in [
                "tetragon-fixture.yaml",
                "tetragon-policy.yaml",
                "network-fixture.yaml",
                "network-policy.yaml",
            ]
        },
        "openControls": [
            "eBPF policy readiness before first workload instruction",
            "IPv6/DNS/external-provider bypass",
            "node-agent/CNI outage behavior",
            "event-loss and kernel compatibility across production nodes",
        ],
        "limitations": [
            "Selected synthetic fixture only; no blanket kernel sandbox guarantee.",
            "Sigkill response verified for security_file_permission on recorded kernel.",
            "Normalized records are unsigned observation; denied access cannot authorize a capability.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    output.with_name("tetragon-normalized-events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in normalized)
    )
    print(json.dumps({"status": report["status"], "checks": len(checks), "report": str(output)}))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    qualify(args.kubeconfig, args.output)
