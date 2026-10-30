"""One httpx client per route host, with bearer auth and a fixed Host header (spec 11.3)."""

import httpx

from agent_runtime.identity import BearerAuth, env, gateway_url, identity, tls_verify


class PolicyDenied(RuntimeError):
    """The gateway or the policy service refused the request (403). Never retried."""


class BudgetExhausted(RuntimeError):
    """The session or agent budget, or the per-sandbox rate limit, refused the request (429)."""


def client_for(
    host_env: str, timeout: float, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """An httpx client whose every request goes to the gateway over TLS with a fixed Host header."""
    verify = tls_verify()  # the internal CA; never False
    return httpx.Client(
        base_url=gateway_url(),
        headers={"Host": env(host_env), "User-Agent": "agent-runtime/1"},
        auth=BearerAuth(identity()),
        verify=verify,
        timeout=timeout,
        follow_redirects=False,  # a redirect would be an attempt to send the token elsewhere
        # Connection-level retries only. The transport carries its own TLS settings: httpx
        # ignores the client's `verify` once a transport is passed.
        transport=transport or httpx.HTTPTransport(retries=2, verify=verify),
    )


_INFERENCE: httpx.Client | None = None


def inference() -> httpx.Client:
    global _INFERENCE
    if _INFERENCE is None:
        _INFERENCE = client_for("AGENT_INFERENCE_HOST", timeout=120.0)
    return _INFERENCE
