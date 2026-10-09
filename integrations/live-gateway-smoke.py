#!/usr/bin/env python3
"""Actual agentgateway v1.6.0 + deterministic MCP receiver + signed JWT tests.

Synthetic local identities only; no production credential, external model or
security qualification is implied. Optional OTLP collector is independently run.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_BINARY_SHA256 = "6cebe8bd57262edce23a65a376d2f5a27b97cdfef609740642edbe75e1e9c685"
TRACE_ID = "7d3b11f0794d447590fbde234eed5264"


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def signed_token(
    key: Path,
    *,
    subject="knowledge-assistant",
    issuer="zetra-local-test",
    audience="zetra-gateway-test",
    expiry=None,
) -> str:
    header = b64(json.dumps({"alg": "RS256", "typ": "JWT", "kid": "zetra-test"}).encode())
    claims = b64(
        json.dumps(
            {
                "sub": subject,
                "iss": issuer,
                "aud": audience,
                "exp": expiry if expiry is not None else int(time.time()) + 120,
                "iat": int(time.time()),
            }
        ).encode()
    )
    signing = f"{header}.{claims}".encode()
    signature = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key)],
        input=signing,
        capture_output=True,
        check=True,
    ).stdout
    return signing.decode() + "." + b64(signature)


class Receiver(BaseHTTPRequestHandler):
    calls = []
    writes = 0

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self):
        self.send_response(405)
        self.end_headers()

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size > 128_000:
            self.send_response(413)
            self.end_headers()
            return
        value = json.loads(self.rfile.read(size))
        method = value.get("method")
        Receiver.calls.append({"method": method, "tool": value.get("params", {}).get("name")})
        if "id" not in value:
            self.send_response(202)
            self.end_headers()
            return
        if method == "initialize":
            requested = value.get("params", {}).get("protocolVersion", "2025-06-18")
            result = {
                "protocolVersion": requested
                if requested in ("2025-06-18", "2025-11-25")
                else "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "zetra-deterministic-receiver", "version": "0.1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": n,
                        "description": n,
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    }
                    for n in ("read_status", "write_status")
                ]
            }
        elif method == "tools/call":
            name = value.get("params", {}).get("name")
            if name == "write_status":
                Receiver.writes += 1
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": "status: healthy" if name == "read_status" else "write committed",
                    }
                ],
                "isError": False,
            }
        elif method == "ping":
            result = {}
        else:
            result = {}
        data = json.dumps({"jsonrpc": "2.0", "id": value["id"], "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MCPClient:
    def __init__(self, url, token=None):
        self.url, self.token, self.session, self.counter = url, token, None, 0

    def call(self, method, params=None):
        self.counter += 1
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
            "traceparent": "00-" + TRACE_ID + "-" + secrets.token_hex(8) + "-01",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        body = {"jsonrpc": "2.0", "id": self.counter, "method": method}
        if params is not None:
            body["params"] = params
        req = urllib.request.Request(self.url, json.dumps(body).encode(), headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                code, response_headers, data = (
                    response.status,
                    response.headers,
                    response.read().decode(),
                )
        except urllib.error.HTTPError as error:
            code, response_headers, data = error.code, error.headers, error.read().decode()
        self.session = response_headers.get("Mcp-Session-Id", self.session)
        if data.startswith("event:") or data.startswith("data:"):
            payloads = [x[5:].strip() for x in data.splitlines() if x.startswith("data:")]
            data = payloads[-1] if payloads else data
        try:
            decoded = json.loads(data)
        except json.JSONDecodeError:
            decoded = {"raw": data[:1000]}
        return code, decoded

    def initialize(self):
        return self.call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "zetra-test", "version": "0.1"},
            },
        )


def wait_port(port, process):
    until = time.monotonic() + 15
    while time.monotonic() < until:
        if process.poll() is not None:
            raise RuntimeError("Gateway exited; inspect gateway log")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Gateway listener did not become ready")


async def temporal_chain(url: str, token: str, output: Path) -> dict:
    """Use actual reference factory, actual SDK worker, and real Temporal test server."""
    from concurrent.futures import ThreadPoolExecutor

    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Replayer, Worker

    from zetra.gateway import GatewayMCPClient
    from zetra.temporal_worker import AgentActivities
    from zetra.temporal_workflow import AgentWorkflow

    audit, attempts = [], []
    client = GatewayMCPClient(
        url, token_provider=lambda: token, audit=audit.append, allow_loopback_http=True
    )
    delegate = AgentActivities(
        ROOT / "examples/gateway-read",
        {},
        gateway_client=client,
        release_id="zetra-local-gateway-1",
    )

    @activity.defn(name="zetra.run_agent")
    def retry_once(request: dict) -> dict:
        attempts.append(activity.info().attempt)
        if activity.info().attempt == 1:
            raise RuntimeError("Injected transient failure before read-only MCP call")
        return delegate.run_agent(request)

    Path("/tmp/zetra-temporal-bin").mkdir(parents=True, exist_ok=True)
    async with await WorkflowEnvironment.start_time_skipping(
        download_dest_dir="/tmp/zetra-temporal-bin"
    ) as env:
        with ThreadPoolExecutor(max_workers=2) as executor:
            async with Worker(
                env.client,
                task_queue="zetra-gateway-test",
                workflows=[AgentWorkflow],
                activities=[retry_once],
                activity_executor=executor,
            ):
                handle = await env.client.start_workflow(
                    AgentWorkflow.run,
                    {},
                    id="zetra-gateway-chain-" + secrets.token_hex(8),
                    task_queue="zetra-gateway-test",
                )
                result = await handle.result()
                assert "healthy" in json.dumps(result), result
                assert attempts == [1, 2], attempts
                assert len(audit) == 1 and audit[0]["outcome"] == "allowed", audit
                assert audit[0]["runId"] == handle.result_run_id, audit
                history = await handle.fetch_history()
                (output / "temporal-gateway-history.json").write_text(history.to_json())
                await Replayer(workflows=[AgentWorkflow]).replay_workflow(history)
    report = {
        "status": "passed",
        "scope": "real-Temporal-test-server-reference-agent-real-gateway-MCP",
        "result": result,
        "activityAttempts": attempts,
        "gatewayAudit": audit,
        "checks": [
            "Reference agent returned actual MCP result",
            "Read-only activity retried once",
            "Run ID propagated into gateway adapter audit",
            "Recorded history replayed",
        ],
        "notQualified": [
            "Kubernetes Temporal service",
            "production TLS/identity",
            "write exactly-once effects",
        ],
    }
    test_server = Path("/tmp/zetra-temporal-bin/temporal-test-server-sdk-python-1.34.0")
    if test_server.is_file():
        report["temporalTestServerSha256"] = hashlib.sha256(test_server.read_bytes()).hexdigest()
    (output / "temporal-gateway-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(args):
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    state = ROOT / ".tools" / "gateway-test-state"
    state.mkdir(parents=True, exist_ok=True)
    binary = Path(args.binary).resolve()
    actual = hashlib.sha256(binary.read_bytes()).hexdigest()
    if actual != EXPECTED_BINARY_SHA256:
        raise RuntimeError("Pinned Darwin ARM64 v1.6.0 binary checksum mismatch")
    key = state / "test-rsa.pem"
    subprocess.run(
        ["openssl", "genrsa", "-out", str(key), "2048"],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    key.chmod(0o600)
    modulus = (
        subprocess.run(
            ["openssl", "rsa", "-in", str(key), "-noout", "-modulus"],
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
        .split("=", 1)[1]
    )
    jwks = state / "jwks.json"
    jwks.write_text(
        json.dumps(
            {
                "keys": [
                    {
                        "kty": "RSA",
                        "kid": "zetra-test",
                        "use": "sig",
                        "alg": "RS256",
                        "n": b64(bytes.fromhex(modulus)),
                        "e": "AQAB",
                    }
                ]
            }
        )
    )
    config = output / "gateway-config.yaml"
    config.write_text(
        f"""# Official standalone v1.6.0 configuration; synthetic test identities.
mcp:
  port: {args.gateway_port}
  dnsRebindingProtection: true
  policies:
    jwtAuth:
      mode: strict
      issuer: zetra-local-test
      audiences: [zetra-gateway-test]
      jwks:
        file: {jwks}
    mcpAuthorization:
      rules:
        - 'jwt.sub == "knowledge-assistant" && mcp.tool.name == "read_status"'
  targets:
    - name: fixture
      mcp:
        host: http://127.0.0.1:{args.backend_port}/mcp
"""
        + (
            f"""frontendPolicies:
  tracing:
    host: {args.collector}
    randomSampling: true
    resources:
      service.name: '"zetra-live-gateway"'
      deployment.environment: '"disposable-local-kind"'
    attributes:
      zetra.release.id: '"x-zetra-release-id" in request.headers ? request.headers["x-zetra-release-id"] : ""'
      zetra.run.id: '"x-zetra-run-id" in request.headers ? request.headers["x-zetra-run-id"] : ""'
      zetra.operation.id: '"x-zetra-operation-id" in request.headers ? request.headers["x-zetra-operation-id"] : ""'
"""
            if args.collector
            else ""
        )
    )
    server = ThreadingHTTPServer(("127.0.0.1", args.backend_port), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log = (output / "gateway.log").open("w")
    proc = subprocess.Popen(
        [str(binary), "-f", str(config)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT
    )
    checks = []

    def check(name, condition, detail):
        checks.append(
            {"name": name, "status": "passed" if condition else "failed", "detail": detail}
        )

    try:
        wait_port(args.gateway_port, proc)
        url = f"http://127.0.0.1:{args.gateway_port}/mcp"
        for name, token in [
            ("missing-token", None),
            ("wrong-issuer", signed_token(key, issuer="wrong-issuer")),
            ("wrong-audience", signed_token(key, audience="wrong-audience")),
            ("expired-token-beyond-leeway", signed_token(key, expiry=int(time.time()) - 3600)),
        ]:
            code, response = MCPClient(url, token).initialize()
            check(name, code in (401, 403), {"httpStatus": code, "response": response})
        valid = signed_token(key)
        forged = valid.rsplit(".", 1)[0] + "." + b64(b"invalid-signature")
        code, response = MCPClient(url, forged).initialize()
        check("forged-signature", code in (401, 403), {"httpStatus": code, "response": response})
        client = MCPClient(url, valid)
        code, response = client.initialize()
        check(
            "valid-initialize",
            code == 200 and "result" in response,
            {"httpStatus": code, "response": response},
        )
        code, response = client.call("tools/list")
        names = [t["name"] for t in response.get("result", {}).get("tools", [])]
        check(
            "tool-list-filtered",
            code == 200
            and any(n.endswith("read_status") for n in names)
            and not any(n.endswith("write_status") for n in names),
            {"names": names},
        )
        permitted = next((n for n in names if n.endswith("read_status")), "read_status")
        denied = permitted.replace("read_status", "write_status")
        code, response = client.call("tools/call", {"name": permitted, "arguments": {}})
        check(
            "allowed-read",
            code == 200 and not response.get("error") and "healthy" in json.dumps(response),
            {"httpStatus": code, "response": response},
        )
        before = Receiver.writes
        code, response = client.call("tools/call", {"name": denied, "arguments": {}})
        check(
            "denied-write-not-forwarded",
            Receiver.writes == before
            and (
                code >= 400
                or bool(response.get("error"))
                or bool(response.get("result", {}).get("isError"))
            ),
            {"httpStatus": code, "response": response, "writes": Receiver.writes},
        )
        wrong = MCPClient(url, signed_token(key, subject="other-agent"))
        wrong.initialize()
        code, response = wrong.call("tools/call", {"name": permitted, "arguments": {}})
        check(
            "wrong-subject-denied",
            code >= 400
            or bool(response.get("error"))
            or bool(response.get("result", {}).get("isError")),
            {"httpStatus": code, "response": response},
        )
        if args.temporal:
            chain = asyncio.run(temporal_chain(url, valid, output))
            check("reference-agent-temporal-gateway-chain", chain["status"] == "passed", chain)
        # Give the real proxy's batch exporter time to deliver when configured.
        if args.collector:
            time.sleep(6)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        server.shutdown()
        server.server_close()
        log.close()
        key.unlink(missing_ok=True)
    report = {
        "createdAt": datetime.now(UTC).isoformat(),
        "scope": "macOS-loopback-live-proxy-with-deterministic-MCP-backend",
        "agentgatewayVersion": "1.6.0",
        "binarySha256": actual,
        "checks": checks,
        "receiverCalls": Receiver.calls,
        "receiverWrites": Receiver.writes,
        "collectorEndpoint": args.collector,
        "status": "passed" if checks and all(c["status"] == "passed" for c in checks) else "failed",
        "injectedTraceId": TRACE_ID,
        "notQualified": [
            "production identity issuance",
            "TLS transport",
            "network/sandbox bypass prevention",
            "production revocation and resource/tenant authorization",
            "Kubernetes controller CRDs",
            "paid model quality or billing",
            "collector export requires separate log assertion",
        ],
        "observations": [
            "An initial token expired by 60 seconds was accepted; default JWT clock leeway must be included in authority freshness objectives. The expiry gate here tests a token expired by one hour."
        ],
    }
    (output / "gateway-report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {"status": report["status"], "checks": len(checks), "receiverWrites": Receiver.writes}
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--binary", default=str(ROOT / ".tools" / "agentgateway"))
    p.add_argument("--output", default=str(ROOT / "integrations" / "gateway-live-output"))
    p.add_argument("--gateway-port", type=int, default=18300)
    p.add_argument("--backend-port", type=int, default=18305)
    p.add_argument("--collector", help="Actual OTLP/gRPC collector endpoint, e.g. 127.0.0.1:14317")
    p.add_argument(
        "--temporal",
        action="store_true",
        help="Execute real reference agent through an ephemeral Temporal SDK test server",
    )
    raise SystemExit(main(p.parse_args()))
