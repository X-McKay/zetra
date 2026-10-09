#!/usr/bin/env python3
"""Assert delivery from real gateway to the real cluster collector debug exporter."""

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path


def main(args):
    output = Path(args.output)
    log = (output / "collector.log").read_text()
    gateway_log = (output / "gateway.log").read_text()
    gateway = json.loads((output / "gateway-report.json").read_text())
    temporal = json.loads((output / "temporal-gateway-report.json").read_text())
    pods = json.loads((output / "collector-pods.json").read_text())
    run_id = temporal["gatewayAudit"][0]["runId"]
    checks = {
        "actual-collector-spans": "Span #" in log and "gen_ai.tool.name: Str(read_status)" in log,
        "injected-traceparent-preserved": gateway["injectedTraceId"] in log,
        "temporal-run-linked": f"zetra.run.id: Str({run_id})" in log,
        "release-linked": "zetra.release.id: Str(zetra-local-gateway-1)" in log,
        "operation-linked": f"zetra.operation.id: Str({run_id}:status-1)" in log,
        "deny-visible": "gen_ai.tool.name: Str(write_status)" in log
        and "http.response.status_code: Int(400)" in log,
        "no-raw-bearer-in-captured-logs": not re.search(
            r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", log + gateway_log
        ),
        "gateway-and-chain-passed": gateway["status"] == temporal["status"] == "passed",
    }
    report = {
        "status": "passed" if all(checks.values()) else "failed",
        "createdAt": datetime.now(UTC).isoformat(),
        "scope": "macOS-reference-agent-and-Temporal-test-server-to-native-gateway-to-MCP-fixture-to-kind-OTel-collector-via-loopback-port-forward",
        "checks": checks,
        "collectorImages": [
            s["imageID"] for p in pods["items"] for s in p["status"]["containerStatuses"]
        ],
        "gatewayBinarySha256": gateway["binarySha256"],
        "collectorLogSha256": hashlib.sha256(log.encode()).hexdigest(),
        "temporalRunId": run_id,
        "notQualified": [
            "single shared Temporal/gateway trace DAG",
            "production telemetry access/retention",
            "universal secret redaction",
            "TLS exporter authentication",
            "all components in Kubernetes",
        ],
        "notes": [
            "Correlation headers are diagnostic metadata, never trusted workload identity.",
            "Log assertions cover this synthetic fixture only. No generic DLP assurance is claimed.",
        ],
    }
    (output / "telemetry-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "checks": checks}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="integrations/gateway-live-output")
    raise SystemExit(main(p.parse_args()))
