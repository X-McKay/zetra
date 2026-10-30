"""MCP client bound to the MCP route (spec 11.5).

mcp SDK 1.30: the transport is `streamable_http_client(url, http_client=...)`, which yields
(read, write, get_session_id). The SDK follows a redirect only when it stays on the request's
origin, which here is always the gateway, so the token never leaves it.
"""

import asyncio
import json
import re
from datetime import timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agent_runtime.gateway import BudgetExhausted, PolicyDenied
from agent_runtime.identity import BearerAuth, env, gateway_url, identity, tls_verify

MAX_ARGS = 32768  # bytes of JSON; the gateway forwards at most 64 KiB to the policy service
MAX_RESULT = 65536
BACKEND = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _client(auth: BearerAuth, transport: httpx.AsyncBaseTransport | None = None):
    verify = tls_verify()
    return httpx.AsyncClient(
        headers={"Host": env("AGENT_MCP_HOST")},
        auth=auth,
        verify=verify,
        timeout=15.0,
        follow_redirects=False,
        transport=transport or httpx.AsyncHTTPTransport(verify=verify),
    )


def _truncate(text: str, limit: int) -> str:
    b = text.encode()
    return text if len(b) <= limit else b[:limit].decode("utf-8", errors="ignore")


async def call_tool(
    backend: str,
    tool: str,
    arguments: dict,
    timeout: float = 25.0,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    if not BACKEND.match(backend):
        raise ValueError("invalid MCP backend name")
    if not isinstance(arguments, dict):
        raise TypeError("tool arguments must be an object")
    if len(json.dumps(arguments).encode()) > MAX_ARGS:
        raise ValueError("tool arguments exceed 32 KiB")
    url = f"{gateway_url()}/{backend}/mcp"
    auth = BearerAuth(identity())
    try:
        async with (
            asyncio.timeout(timeout),
            _client(auth, transport) as http,
            streamable_http_client(url, http_client=http) as (read, write, _),
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session,
        ):
            await session.initialize()
            result = await session.call_tool(tool, arguments)
    except Exception:
        # The transport reports HTTP failures in version-specific ways; the status is the fact.
        if auth.status == 403:
            raise PolicyDenied(f"MCP {backend} refused the call") from None
        if auth.status == 429:
            raise BudgetExhausted(f"MCP {backend} call over budget") from None
        raise
    if result.isError:
        raise RuntimeError(f"MCP tool {tool[:128]} returned an error")
    text = "\n".join(part.text for part in result.content if getattr(part, "type", "") == "text")
    # Truncate before it enters state.
    return {"tool": tool, "backend": backend, "text": _truncate(text, MAX_RESULT)}
