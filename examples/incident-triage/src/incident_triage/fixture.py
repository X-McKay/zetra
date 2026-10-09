"""Offline MCP wire fixture, no sockets, JWT verifier, service or model.

The real GatewayMCPClient still parses its JSON-RPC and session responses.
Live gateway authorization is separately recorded in integrations/.
"""

import io
import json
from urllib.request import OpenerDirector, Request

from zetra.gateway import PROTOCOL, GatewayMCPClient
from zetra.runtime import Denied

RUNBOOK = "Check the health dashboard, preserve correlation IDs, and ask the service owner to investigate. Do not restart or modify production services."
INCIDENT = {
    "incidentId": "INC-2026-0042",
    "service": "checkout-api",
    "severity": "warning",
    "summary": "Synthetic checkout latency alert exceeded the review threshold.",
}
RUN_ID = "run-INC-2026-0042-001"


class FixtureResponse(io.BytesIO):
    def __init__(self, message: dict, status: int = 200):
        super().__init__(json.dumps(message).encode())
        self.status = status
        self.headers = {
            "Content-Type": "application/json",
            "MCP-Session-Id": "offline-fixture-session",
        }


class OfflineMCPWire(OpenerDirector):
    def __init__(self, status: str = "degraded"):
        super().__init__()
        self.status, self.calls = status, []

    def open(self, fullurl, data=None, timeout=10):
        if not isinstance(fullurl, Request) or data is not None:
            raise Denied("Offline fixture requires a prepared POST Request")
        body = fullurl.data
        if not isinstance(body, bytes):
            raise Denied("Offline fixture requires JSON bytes")
        message = json.loads(body)
        self.calls.append(message["method"])
        if message["method"] == "initialize":
            return FixtureResponse(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}}},
                }
            )
        if message["method"] == "notifications/initialized":
            return FixtureResponse({}, status=202)
        if (
            message["method"] != "tools/call"
            or message["params"]["name"] != "read_status"
            or message["params"]["arguments"]
        ):
            raise Denied("Offline fixture supports read_status({}) only")
        return FixtureResponse(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "content": [{"type": "text", "text": "status: " + self.status}],
                    "isError": False,
                },
            }
        )


def fixture_client(audit, status: str = "degraded") -> tuple[GatewayMCPClient, OfflineMCPWire]:
    client = GatewayMCPClient(
        "https://offline-gateway.example.invalid/mcp",
        token_provider=lambda: "offline-fixture-token",
        audit=audit,
    )
    wire = OfflineMCPWire(status)
    client.opener = wire
    return client, wire
