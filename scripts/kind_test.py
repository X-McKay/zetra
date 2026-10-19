"""Scoped port-forwards plus real cluster tests; no default kubeconfig mutation."""

import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

root = Path(__file__).resolve().parents[1]
state = Path(os.environ.get("BRIDGE_STATE_DIR", root / ".state")).resolve()
kube = ["kubectl", "--kubeconfig", str(state / "kubeconfig")]


@contextmanager
def forward(namespace, service, port):
    path = state / (service + "-forward.log")
    with path.open("w") as log:
        process = subprocess.Popen(
            [
                *kube,
                "-n",
                namespace,
                "port-forward",
                "service/" + service,
                f":{port}",
                "--address=127.0.0.1",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(100):
                match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", path.read_text())
                if match:
                    break
                if process.poll() is not None:
                    raise RuntimeError(path.read_text())
                time.sleep(0.1)
            else:
                raise TimeoutError("port-forward readiness")
            yield "127.0.0.1:" + match.group(1)
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


with (
    forward("temporal", "temporal-frontend", 7233) as temporal,
    forward("openshell", "openshell", 8080) as openshell,
):
    env = os.environ.copy()
    env.update(
        TEMPORAL_ADDRESS=temporal,
        OPENSHELL_ADDRESS=openshell,
        OPENSHELL_TLS_DIR=str(state / "tls"),
        BRIDGE_KUBECONFIG=str(state / "kubeconfig"),
    )
    for namespace, service, key in [
        ("temporal", "temporal-frontend", "TEMPORAL_CLUSTER_IP"),
        ("openshell", "openshell", "OPENSHELL_CLUSTER_IP"),
    ]:
        env[key] = subprocess.check_output(
            [*kube, "-n", namespace, "get", "service", service, "-o", "jsonpath={.spec.clusterIP}"],
            text=True,
        )
    args = sys.argv[1:] or ["tests"]
    raise SystemExit(
        subprocess.call(
            [sys.executable, "-m", "pytest", *args, "-q", "--junitxml=" + str(state / "tests.xml")],
            cwd=root,
            env=env,
        )
    )
