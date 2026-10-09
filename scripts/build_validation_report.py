"""Index observed lab evidence without granting deployment authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REPORTS = {
    "contributor-tooling": "docs/research/tooling-validation.json",
    "document-rendering": "docs/research/document-qa.json",
    "incident-triage-reference": "docs/research/incident-triage-validation.json",
    "document-preview": "docs/research/preview-validation.json",
    "kernel-and-network": "integrations/evidence/kernel-network-report.json",
    "openshell": "integrations/openshell-live-output/report.json",
    "gateway": "integrations/gateway-live-output/gateway-report.json",
    "temporal-gateway-chain": "integrations/gateway-live-output/temporal-gateway-report.json",
    "telemetry": "integrations/gateway-live-output/telemetry-report.json",
    "temporal-local-replay": "integrations/evidence/temporal-local-test.json",
    "worker-image": "integrations/evidence/worker-image.json",
    "native-render-server": "integrations/evidence/render-server-report.json",
    "kubernetes-worker-temporal": "integrations/evidence/worker-temporal-kubernetes-report.json",
    "platform-bootstrap": "integrations/evidence/platform-bootstrap.json",
}


def main() -> None:
    entries = []
    for name, relative in REPORTS.items():
        path = ROOT / relative
        if not path.is_file():
            raise RuntimeError(f"Missing observed report: {relative}")
        raw = path.read_bytes()
        report = json.loads(raw)
        checks = report.get("checks", report.get("tests", []))
        entry = {
            "component": name,
            "report": relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "status": report.get("status", "unreported"),
            "scope": report.get("scope", report.get("limitations", [])),
            "checkCount": len(checks),
            "openControls": report.get(
                "notQualified", report.get("openControls", report.get("limitations", []))
            ),
        }
        entries.append(entry)
    result = {
        "apiVersion": "zetra.dev/lab-evidence-index-v1alpha1",
        "status": "passed" if all(e["status"] == "passed" for e in entries) else "incomplete",
        "scope": "Recorded local fixture and contributor checks; hybrid host/kind topology",
        "authorizesDeployment": False,
        "productionQualified": False,
        "components": entries,
        "interpretation": [
            "Checks overlap; check counts are not independent statistical samples.",
            "Hashes bind recorded reports, not trusted provenance, freshness or signer identity.",
            "Installation and read-only discovery are not enforcement tests.",
            "See each report and reproduction guide for its actual topology and limitations.",
        ],
        "productionGates": [
            "Authenticated enterprise identities, approvers and workload credential exchange",
            "TLS and tenant/resource authorization on every production hop",
            "Signed build, evaluation, integration and catalog attestations with admission enforcement",
            "Distributed revocation, kill switches and durable concurrent budget accounting",
            "Production Kubernetes Temporal service and worker crash/recovery qualification",
            "OpenShell Kubernetes maturity decision and production isolation tuple",
            "Kernel/CNI startup, outage, event-loss and bypass tests on every supported node tuple",
            "Real model capability, quality, adversarial and business-outcome evaluation",
            "Governed catalog/CD publication and business-function telemetry access/retention",
        ],
    }
    target = ROOT / "integrations/validation-report.json"
    target.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "reports": len(entries)}))


if __name__ == "__main__":
    main()
