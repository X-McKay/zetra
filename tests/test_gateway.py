import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

from zetra.gateway import PROTOCOL, GatewayMCPClient, NoRedirect
from zetra.runtime import Denied, RuntimeDependencies


class Response(io.BytesIO):
    def __init__(self, message, content_type="application/json", session=None, status=200):
        body = message.encode() if isinstance(message, str) else json.dumps(message).encode()
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": content_type}
        if session:
            self.headers["MCP-Session-Id"] = session


class GatewayWire:
    def __init__(self, *, sse=False, error=None, oversized=False):
        self.requests = []
        self.sse, self.error, self.oversized = sse, error, oversized

    def open(self, request, timeout):
        message = json.loads(request.data)
        self.requests.append((request, message))
        if self.error:
            raise HTTPError(request.full_url, self.error, "blocked", Message(), None)
        if message["method"] == "initialize":
            return Response(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}}},
                },
                session="opaque-session",
            )
        if message["method"] == "notifications/initialized":
            return Response({}, status=202)
        result = {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "content": [
                    {"type": "text", "text": "x" * 2000 if self.oversized else "status: healthy"}
                ],
                "isError": False,
            },
        }
        return (
            Response("event: message\ndata: " + json.dumps(result) + "\n\n", "text/event-stream")
            if self.sse
            else Response(result)
        )


class GatewayTests(unittest.TestCase):
    def client(self, **kwargs):
        self.audit = []
        client = GatewayMCPClient(
            "http://127.0.0.1:18300/mcp",
            token_provider=lambda: "sensitive.jwt.token",
            audit=self.audit.append,
            allow_loopback_http=True,
            **kwargs,
        )
        return client

    def call(self, client):
        return client.call(
            "read_status", {}, release_id="sha256:release", run_id="run-1", operation_id="read-1"
        )

    def test_explicit_https_and_loopback_and_known_tool_only(self):
        for endpoint in (
            "http://gateway.svc/mcp",
            "http://localhost:18300/mcp",
            "https://user:pass@gateway/mcp",
            "https://gateway/mcp#fragment",
        ):
            with self.assertRaises(Denied):
                GatewayMCPClient(endpoint, token_provider=lambda: "token")
        with self.assertRaises(Denied):
            self.client(allowed_tools={"write_status": "tool.write"})
        client = self.client()
        wire = GatewayWire()
        client.opener = wire
        for tool, arguments in (("write_status", {}), ("read_status", {"shell": "execute"})):
            with self.assertRaises(Denied):
                client.call(tool, arguments, release_id="r", run_id="r", operation_id="r")
        self.assertEqual(wire.requests, [])

    def test_real_mcp_wire_contract_session_correlation_and_redacted_audit(self):
        client = self.client()
        client.opener = wire = GatewayWire()
        result = self.call(client)
        self.assertEqual(result["content"][0]["text"], "status: healthy")
        self.assertEqual(
            [x[1]["method"] for x in wire.requests],
            ["initialize", "notifications/initialized", "tools/call"],
        )
        headers = dict(wire.requests[-1][0].header_items())
        self.assertEqual(headers["Authorization"], "Bearer sensitive.jwt.token")
        self.assertEqual(headers["Mcp-session-id"], "opaque-session")
        self.assertEqual(headers["X-zetra-operation-id"], "read-1")
        self.assertNotIn("sensitive", json.dumps(self.audit))
        self.assertEqual(self.audit[0]["outcome"], "allowed")
        self.call(client)
        self.assertEqual(len(wire.requests), 4)  # reuse initialized session

    def test_sse_bounded_response_and_http_denial(self):
        client = self.client()
        client.opener = GatewayWire(sse=True)
        self.assertEqual(self.call(client)["content"][0]["text"], "status: healthy")
        client = self.client(max_response_bytes=1024)
        client.opener = GatewayWire(oversized=True)
        with self.assertRaisesRegex(Denied, "size bound"):
            self.call(client)
        client = self.client()
        client.opener = GatewayWire(error=401)
        with self.assertRaisesRegex(Denied, "HTTP status 401") as caught:
            self.call(client)
        self.assertNotIn("sensitive", str(caught.exception))
        self.assertEqual(self.audit[0]["outcome"], "denied")

    def test_redirect_never_forwards_identity(self):
        with self.assertRaisesRegex(Denied, "redirects"):
            NoRedirect().redirect_request(
                None, None, 302, "redirect", {}, "https://attacker.invalid"
            )

    def test_capability_and_unconfigured_client_denied_before_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            deps = RuntimeDependencies(frozenset(), 3, 30, 0.05, Path(directory) / "STOP")
            with self.assertRaisesRegex(Denied, "Capability denied"):
                deps.read_status()
            deps.capabilities = frozenset({"mcp.read_status"})
            with self.assertRaisesRegex(Denied, "No qualified"):
                deps.read_status()


if __name__ == "__main__":
    unittest.main()
