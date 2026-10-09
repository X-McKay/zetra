#!/usr/bin/env python3
"""Real OpenShell0.1.2 strict sandbox qualification in disposable kind only.

Requires the separately installed official Helm chart and Agent Sandbox CRDs.
Private certificates remain in ignored .tools; never print secret material.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def execute(command, *, env=None, check=True, timeout=60):
    process = subprocess.run(
        command, cwd=ROOT, env=env, text=True, capture_output=True, timeout=timeout
    )
    if check and process.returncode:
        raise RuntimeError(
            f"Command failed ({process.returncode}): {' '.join(command[:5])}; {process.stderr[:1500]}"
        )
    return process


def main(args):
    output = ROOT / "integrations/openshell-live-output"
    output.mkdir(parents=True, exist_ok=True)
    kubeconfig = ROOT / ".tools/zetra-kubeconfig"
    if not kubeconfig.is_file():
        raise RuntimeError("Dedicated disposable cluster kubeconfig required")
    kubectl = [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        "kind-zetra-validation",
        "-n",
        "zetra-openshell",
    ]
    env = dict(
        os.environ,
        XDG_CONFIG_HOME=str(ROOT / ".tools/config"),
        XDG_STATE_HOME=str(ROOT / ".tools/state"),
        XDG_DATA_HOME=str(ROOT / ".tools/data"),
    )
    openshell = [str(ROOT / ".tools/openshell"), "-g", "zetra-local"]
    version = execute([str(ROOT / ".tools/openshell"), "--version"]).stdout.strip()
    if version != "openshell 0.1.2":
        raise RuntimeError("OpenShell0.1.2 CLI required by this qualification")
    tls = json.loads(
        execute([*kubectl, "get", "secret", "openshell-client-tls", "-o", "json"]).stdout
    )["data"]
    mtls = ROOT / ".tools/config/openshell/gateways/zetra-local/mtls"
    mtls.mkdir(parents=True, exist_ok=True)
    mtls.chmod(0o700)
    for name in ("ca.crt", "tls.crt", "tls.key"):
        target = mtls / name
        target.write_bytes(base64.b64decode(tls[name]))
        target.chmod(0o600)
    source = ROOT / ".tools/openshell-fixture"
    (source / "src/zetra").mkdir(parents=True, exist_ok=True)
    (source / "src/knowledge").mkdir(parents=True, exist_ok=True)
    copies = {
        "src/zetra/runtime.py": "src/zetra/runtime.py",
        "src/zetra/__init__.py": "src/zetra/__init__.py",
        "examples/knowledge/src/knowledge/factory.py": "src/knowledge/factory.py",
        "examples/knowledge/src/knowledge/__init__.py": "src/knowledge/__init__.py",
        "integrations/openshell-probe.py": "probe.py",
    }
    hashes = {}
    for original, dest in copies.items():
        shutil.copyfile(ROOT / original, source / dest)
        hashes[original] = hashlib.sha256((ROOT / original).read_bytes()).hexdigest()
    (output / "source-hashes.json").write_text(json.dumps(hashes, indent=2) + "\n")
    # Only this fixture's code directory is uploaded; --no-git-ignore does not
    # include surrounding .tools credentials or any other repository contents.
    forward_log = (output / "port-forward.log").open("w")
    forward = subprocess.Popen(
        [*kubectl, "port-forward", "service/openshell", "18080:8080", "--address", "127.0.0.1"],
        cwd=ROOT,
        stdout=forward_log,
        stderr=subprocess.STDOUT,
    )
    created = False
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if forward.poll() is not None:
                raise RuntimeError(
                    "OpenShell port-forward exited; port18080 may already be occupied"
                )
            try:
                with socket.create_connection(("127.0.0.1", 18080), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("OpenShell listener unavailable")
        registrations = json.loads(
            execute(
                [str(ROOT / ".tools/openshell"), "gateway", "list", "-o", "json"], env=env
            ).stdout
        )
        existing = next((g for g in registrations if g["name"] == "zetra-local"), None)
        if existing:
            if (
                existing["endpoint"] != "https://localhost:18080"
                or existing["auth"] != "mtls"
                or existing["type"] != "local"
            ):
                raise RuntimeError(
                    "Existing local gateway registration does not match this fixture"
                )
        else:
            execute(
                [
                    str(ROOT / ".tools/openshell"),
                    "gateway",
                    "add",
                    "https://localhost:18080",
                    "--local",
                    "--name",
                    "zetra-local",
                ],
                env=env,
            )
        execute([*openshell, "status"], env=env)
        command = [
            *openshell,
            "sandbox",
            "create",
            "--name",
            args.name,
            "--from",
            "docker.io/library/python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36",
            "--cpu",
            "500m",
            "--memory",
            "512Mi",
            "--policy",
            str(ROOT / "integrations/openshell-strict-policy.yaml"),
            "--no-auto-providers",
            "--detach",
            "--",
            "/bin/sleep",
            "3600",
        ]
        creation = execute(command, env=env, timeout=120)
        created = True
        (output / "create.log").write_text(creation.stdout + creation.stderr)
        execute(
            [
                *openshell,
                "sandbox",
                "upload",
                "--no-git-ignore",
                args.name,
                str(source),
                "/sandbox",
            ],
            env=env,
        )
        probe = execute(
            [
                *openshell,
                "sandbox",
                "exec",
                "-n",
                args.name,
                "--no-login-shell",
                "--no-tty",
                "--timeout",
                "30",
                "--",
                "python",
                "/sandbox/openshell-fixture/probe.py",
            ],
            env=env,
            check=False,
        )
        (output / "probe.json").write_text(probe.stdout)
        probe_result = json.loads(probe.stdout)
        policy = json.loads(
            execute(
                [*openshell, "policy", "get", args.name, "--full", "-o", "json"], env=env
            ).stdout
        )
        (output / "effective-policy.json").write_text(json.dumps(policy, indent=2) + "\n")
        pods = json.loads(execute([*kubectl, "get", "pods", "-o", "json"]).stdout)
        workload = next(
            p for p in pods["items"] if p["metadata"]["name"] == "default--" + args.name
        )
        pair = workload["metadata"]["labels"]["openshell.ai/boundary-pair"]
        supervisor = next(
            p
            for p in pods["items"]
            if p["metadata"]["labels"].get("openshell.ai/boundary-pair") == pair
            and p["metadata"]["labels"].get("openshell.ai/boundary-role") == "supervisor"
        )
        logs = execute(
            [*kubectl, "logs", supervisor["metadata"]["name"], "--all-containers", "--timestamps"]
        ).stdout
        (output / "supervisor.log").write_text(logs)
        details = {
            "status": "passed"
            if probe.returncode == 0
            and probe_result["status"] == "passed"
            and policy["policy"]["landlock"]["compatibility"] == "hard_requirement"
            and "transparent_tcp_policy_denied" in logs
            and policy["hash"] in logs
            else "failed",
            "createdAt": datetime.now(UTC).isoformat(),
            "sandbox": args.name,
            "scope": "actual-OpenShell-supervised-reference-agent-in-disposable-kind",
            "cliVersion": version,
            "cliSha256": hashlib.sha256((ROOT / ".tools/openshell").read_bytes()).hexdigest(),
            "effectivePolicyHash": policy["hash"],
            "landlockCompatibility": "hard_requirement",
            "probe": probe_result,
            "supervisorNetworkDenialObserved": "transparent_tcp_policy_denied" in logs,
            "initialPolicyAcknowledged": policy["hash"] in logs,
            "images": {
                p["metadata"]["name"]: [
                    s["imageID"] for s in p["status"].get("containerStatuses", [])
                ]
                for p in (workload, supervisor)
            },
            "sourceHashes": hashes,
            "notQualified": [
                "production principal authentication; local gateway permits unauthenticated users after mTLS transport",
                "user namespace isolation (disabled in this local tuple)",
                "provider credential exchange",
                "allowed external L7 routes",
                "policy revocation/hot reload",
                "multi-tenant/business authorization",
                "first-instruction race or privileged-node compromise",
            ],
            "fixtureOnly": True,
        }
        (output / "report.json").write_text(json.dumps(details, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "status": details["status"],
                    "checks": len(probe_result["checks"]),
                    "sandbox": args.name,
                }
            ),
            flush=True,
        )
        return 0 if details["status"] == "passed" else 1
    finally:
        try:
            if created and not args.keep_sandbox:
                try:
                    cleanup = execute(
                        [*openshell, "sandbox", "delete", args.name],
                        env=env,
                        check=False,
                        timeout=45,
                    )
                    (output / "cleanup.log").write_text(cleanup.stdout + cleanup.stderr)
                except subprocess.TimeoutExpired:
                    (output / "cleanup.log").write_text(
                        "Sandbox deletion exceeded45s; inspect dedicated disposable namespace.\n"
                    )
        finally:
            forward.terminate()
            try:
                forward.wait(timeout=5)
            except subprocess.TimeoutExpired:
                forward.kill()
                forward.wait()
            forward_log.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--name", default="zetra-" + secrets.token_hex(3))
    p.add_argument("--keep-sandbox", action="store_true")
    raise SystemExit(main(p.parse_args()))
