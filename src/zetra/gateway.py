"""Restricted real Streamable HTTP MCP adapter, pinned to 2025-11-25.

Only the reviewed read_status({}) tool is exposed. Production endpoint policy,
workload JWT verification/revocation and authorization are gateway duties.
This client has no generic tool escape hatch and refuses redirects.
"""

from __future__ import annotations

import json
import re
import ssl
import threading
import time
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from .runtime import Denied, canonical

PROTOCOL = "2025-11-25"
TOOLS = {"read_status": "mcp.read_status"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Denied("Gateway redirects are forbidden")


class GatewayMCPClient:
    def __init__(
        self,
        endpoint: str,
        *,
        token_provider: Callable[[], str],
        audit: Callable[[dict], None] | None = None,
        allow_loopback_http: bool = False,
        timeout_seconds: float = 10,
        max_response_bytes: int = 262144,
        ca_file: str | None = None,
        allowed_tools: dict[str, str] | None = None,
    ):
        parsed = urlsplit(endpoint)
        if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
            raise Denied("Gateway endpoint must have a hostname and no credentials/fragment")
        if parsed.scheme != "https" and not (
            allow_loopback_http
            and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise Denied("Gateway requires HTTPS; plaintext allowed only for explicit loopback lab")
        if (
            not parsed.path
            or not 0 < timeout_seconds <= 30
            or type(max_response_bytes) is not int
            or not 1024 <= max_response_bytes <= 1048576
        ):
            raise Denied(
                "MCP endpoint path, timeout <=30 seconds and response bound 1 KB..1 MB required"
            )
        tools = dict(TOOLS if allowed_tools is None else allowed_tools)
        if not tools or any(TOOLS.get(tool) != cap for tool, cap in tools.items()):
            raise Denied("Only reviewed read_status -> mcp.read_status tool mapping is supported")
        self.endpoint, self.token_provider, self.audit = endpoint, token_provider, audit
        self.timeout_seconds, self.max_response_bytes, self.allowed_tools = (
            timeout_seconds,
            max_response_bytes,
            tools,
        )
        self.opener = build_opener(
            ProxyHandler({}),
            HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)),
            NoRedirect(),
        )
        self._session: str | None = None
        self._next_id = 0
        self._lock = threading.Lock()
        self._initialized = False

    @staticmethod
    def correlation(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.:/@-]{1,128}", value):
            raise Denied("Correlation IDs must be 1..128 safe ASCII characters")
        return value

    @staticmethod
    def decode(text: str) -> dict:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise Denied("Duplicate gateway JSON key")
                result[key] = value
            return result

        try:
            result = json.loads(
                text,
                object_pairs_hook=unique,
                parse_constant=lambda _: (_ for _ in ()).throw(
                    Denied("Nonfinite gateway JSON value")
                ),
            )
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise Denied("Gateway response is invalid JSON") from exc
        if not isinstance(result, dict):
            raise Denied("Gateway response must be an object")
        return result

    def _post(
        self, method: str, params: dict, correlation: dict, *, notification: bool = False
    ) -> dict:
        token = self.token_provider()
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9\-._~+/]+=*", token)
            or len(token) > 8192
        ):
            raise Denied("Workload bearer token missing or malformed")
        self._next_id += 1
        request_id = self._next_id
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = request_id
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + token,
            "MCP-Protocol-Version": PROTOCOL,
            **{f"X-Zetra-{key}": value for key, value in correlation.items()},
        }
        if self._session:
            headers["MCP-Session-Id"] = self._session
        req = Request(
            self.endpoint, data=canonical(message).encode(), headers=headers, method="POST"
        )
        try:
            with self.opener.open(req, timeout=self.timeout_seconds) as response:
                if notification:
                    if response.status not in {200, 202, 204}:
                        raise Denied("Gateway notification rejected")
                    return {}
                session = response.headers.get("MCP-Session-Id")
                if session:
                    if len(session) > 512 or not re.fullmatch(r"[!-~]+", session):
                        raise Denied("Invalid gateway session identifier")
                    self._session = session
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
                if content_type == "application/json":
                    deadline = time.monotonic() + self.timeout_seconds
                    body = b""
                    while time.monotonic() < deadline:
                        chunk = response.read1(min(4096, self.max_response_bytes - len(body) + 1))
                        body += chunk
                        if len(body) > self.max_response_bytes:
                            raise Denied("Gateway response exceeds size bound")
                        if not chunk:
                            break
                    else:
                        raise Denied("Gateway JSON response timed out")
                    result = self.decode(body.decode("utf-8"))
                elif content_type == "text/event-stream":
                    total, buffer, result = 0, b"", None
                    deadline = time.monotonic() + self.timeout_seconds
                    while time.monotonic() < deadline:
                        chunk = response.read1(min(self.max_response_bytes - total + 1, 4096))
                        total += len(chunk)
                        if total > self.max_response_bytes:
                            raise Denied("Gateway SSE response exceeds size bound")
                        if not chunk:
                            break
                        buffer += chunk.replace(b"\r\n", b"\n")
                        while b"\n\n" in buffer:
                            block, buffer = buffer.split(b"\n\n", 1)
                            data = [
                                line[5:].strip().decode("utf-8")
                                for line in block.split(b"\n")
                                if line.startswith(b"data:")
                            ]
                            if not data:
                                continue
                            candidate = self.decode("\n".join(data))
                            if candidate.get("id") == request_id:
                                result = candidate
                                break
                        if result is not None:
                            break
                    if result is None:
                        raise Denied("Gateway SSE result missing or timed out")
                else:
                    raise Denied("Unsupported gateway response Content-Type")
        except HTTPError as exc:
            # Never copy response bodies, headers or tokens into exceptions.
            raise Denied(f"Gateway HTTP status {exc.code}") from None
        except Denied:
            raise
        except (URLError, TimeoutError, OSError, UnicodeError):
            raise Denied("Gateway transport failed or timed out") from None
        if (
            result.get("jsonrpc") != "2.0"
            or type(result.get("id")) is not int
            or result.get("id") != request_id
            or "error" in result
            or not isinstance(result.get("result"), dict)
        ):
            raise Denied("Gateway JSON-RPC result failed contract")
        return result["result"]

    def call(
        self, tool: str, arguments: dict, *, release_id: str, run_id: str, operation_id: str
    ) -> dict:
        if tool not in self.allowed_tools or not isinstance(arguments, dict) or arguments:
            raise Denied("Only reviewed read_status with empty arguments is allowed")
        correlation = {
            "Release-Id": self.correlation(release_id),
            "Run-Id": self.correlation(run_id),
            "Operation-Id": self.correlation(operation_id),
        }
        event = {
            "event": "gateway.tool_call",
            "tool": tool,
            "releaseId": release_id,
            "runId": run_id,
            "operationId": operation_id,
        }
        with self._lock:
            try:
                if not self._initialized:
                    result = self._post(
                        "initialize",
                        {
                            "protocolVersion": PROTOCOL,
                            "capabilities": {},
                            "clientInfo": {"name": "zetra", "version": "0.1.0"},
                        },
                        correlation,
                    )
                    if result.get("protocolVersion") != PROTOCOL or not isinstance(
                        result.get("capabilities"), dict
                    ):
                        raise Denied("Gateway MCP protocol version or capabilities mismatch")
                    self._post("notifications/initialized", {}, correlation, notification=True)
                    self._initialized = True
                result = self._post(
                    "tools/call",
                    {"name": tool, "arguments": arguments, "_meta": {"zetra": event}},
                    correlation,
                )
                content = result.get("content")
                if (
                    result.get("isError", False) is not False
                    or not isinstance(content, list)
                    or not 1 <= len(content) <= 8
                ):
                    raise Denied("MCP tool result failed or content missing")
                if any(
                    not isinstance(item, dict)
                    or item.get("type") != "text"
                    or not isinstance(item.get("text"), str)
                    or len(item["text"]) > 4000
                    for item in content
                ):
                    raise Denied("read_status output requires bounded text content")
                if self.audit:
                    self.audit({**event, "outcome": "allowed"})
                return {
                    "content": [{"type": "text", "text": item["text"]} for item in content],
                    "isError": False,
                }
            except Denied:
                if self.audit:
                    self.audit({**event, "outcome": "denied"})
                raise
