"""Server-validate a withheld native release render in the disposable kind cluster."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    kubeconfig = ROOT / ".tools/zetra-kubeconfig"
    kubectl = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", "kind-zetra-validation"]
    image = json.loads((ROOT / "integrations/evidence/worker-image.json").read_text())[
        "kindImageReference"
    ]
    work = ROOT / ".tools/render-qualification"
    work.mkdir(parents=True, exist_ok=True)
    evidence, rendered = work / "evidence.json", work / "release.yaml"
    namespace = "zetra-render-test"

    def run(command: list[str]) -> str:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr[:2000])
        return result.stdout

    # A pre-existing namespace is an error; cleanup removes only our new namespace.
    run([*kubectl, "create", "namespace", namespace])
    try:
        cli = str(ROOT / ".venv/bin/zetra")
        run([cli, "eval", "examples/knowledge", "--output", str(evidence)])
        run(
            [
                cli,
                "render",
                "examples/knowledge",
                "--evidence",
                str(evidence),
                "--image",
                image,
                "--namespace",
                namespace,
                "--gateway-namespace",
                "zetra-gateway-test",
                "--gateway-label",
                "app=agentgateway",
                "--output",
                str(rendered),
            ]
        )
        objects = list(yaml.safe_load_all(rendered.read_text()))
        deployment = next(obj for obj in objects if obj["kind"] == "Deployment")
        assert deployment["spec"]["replicas"] == 0
        assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == image
        accepted = run([*kubectl, "apply", "--dry-run=server", "-f", str(rendered), "-o", "name"])
        assert len(accepted.strip().splitlines()) == 4
        report = {
            "status": "passed",
            "checkedAt": datetime.now(UTC).isoformat(),
            "scope": "Native Kubernetes API server dry-run of a withheld release candidate",
            "context": "kind-zetra-validation",
            "image": image,
            "renderSha256": hashlib.sha256(rendered.read_bytes()).hexdigest(),
            "acceptedResources": accepted.strip().splitlines(),
            "checks": [
                "Four native resources accepted by server",
                "Zero replicas preserved",
                "Actual immutable built image reference",
            ],
            "notQualified": [
                "No workload activation",
                "No signed admission verification",
                "No gateway or Temporal environment binding",
            ],
        }
        (ROOT / "integrations/evidence/render-server-report.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )
        print(json.dumps({"status": report["status"], "resources": 4, "replicas": 0}))
    finally:
        run([*kubectl, "delete", "namespace", namespace, "--wait=false"])


if __name__ == "__main__":
    main()
