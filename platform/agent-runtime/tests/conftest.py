import pytest

from agent_runtime import gateway, identity
from tests.fakes import projected_token

GATEWAY = "https://10.43.250.10:443"


@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    """The 5.2 pod environment, with the sandbox and app directories under tmp_path."""
    token = tmp_path / "identity" / "token"
    token.parent.mkdir()
    token.write_text(projected_token(0))
    values = {
        "AGENT_GATEWAY_URL": GATEWAY,
        "AGENT_TOKEN_HOST": "token.agents.internal",
        "AGENT_INFERENCE_HOST": "inference.agents.internal",
        "AGENT_MCP_HOST": "mcp.agents.internal",
        "AGENT_CLIENT_ID": "agent-tenant-test-conformance",
        "AGENT_TOKEN_PATH": str(token),
        "AGENT_TOKEN_ENDPOINT_PATH": "/v1/token",
        "AGENT_MODEL_ROUTE": "/models/llm-main",
        "AGENT_TOKENS_PER_SESSION": "200000",
        "AGENT_SANDBOX_DIR": str(tmp_path / "sandbox"),
        "AGENT_APP_DIR": str(tmp_path / "app"),
    }
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AGENT_GATEWAY_CA", raising=False)
    monkeypatch.setattr(identity, "_IDENTITY", None)
    monkeypatch.setattr(gateway, "_INFERENCE", None)
    return values
