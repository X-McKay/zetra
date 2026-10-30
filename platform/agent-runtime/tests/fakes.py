"""Test doubles: JWT-shaped tokens, a TLS CA, a fake gateway and a fake agent image."""

import base64
import datetime
import ipaddress
import json
import re
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

SECRET_PATTERNS = [
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"Bearer [A-Za-z0-9._~+/=-]{20,}"),
]


def assert_redacted(*texts: str) -> None:
    for text in texts:
        for p in SECRET_PATTERNS:
            assert not p.search(text or ""), f"secret-shaped value leaked: {p.pattern}"


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def make_jwt(**claims) -> str:
    return f"{_b64({'alg': 'RS256', 'typ': 'JWT'})}.{_b64(claims)}.c2lnbmF0dXJlLXNpZ25hdHVyZQ"


def agent_token(n: int = 0, aud=("agent-gateway", "agent-policy-service"), ttl: int = 300) -> str:
    now = int(time.time())
    return make_jwt(
        aud=list(aud),
        client_type="agent",
        jti=f"jti-{n}",
        iat=now,
        exp=now + ttl,
        agent_id="tenant-test/conformance",
    )


def projected_token(n: int = 0) -> str:
    return make_jwt(
        aud=["https://idp.example.com/realms/agents"],
        jti=f"proj-{n}",
        sub="system:serviceaccount:tenant-test:agent-conformance",
    )


def make_ca(directory: Path) -> tuple[Path, Path, Path]:
    """A CA and a server certificate for 127.0.0.1; returns (ca.crt, server.crt, server.key)."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agents")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(2)
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    paths = directory / "ca.crt", directory / "server.crt", directory / "server.key"
    paths[0].write_bytes(ca.public_bytes(pem))
    paths[1].write_bytes(cert.public_bytes(pem))
    paths[2].write_bytes(
        key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return paths


def chat_response(
    content: str | None = None, tool_calls: list | None = None, usage=(10, 5)
) -> dict:
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [
            {
                "id": f"call-{i}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for i, (name, args) in enumerate(tool_calls)
        ]
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}
        ],
        "usage": {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": sum(usage),
        },
    }


class FakeGateway:
    """A TLS server playing the gateway's token and inference routes."""

    def __init__(self, cert: Path, key: Path) -> None:
        self.token_requests: list[dict] = []
        self.chat_requests: list[dict] = []
        self.chat_replies: list[tuple[int, dict]] = []
        self.issued = 0
        gw = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                host = self.headers.get("Host")
                if self.path == "/v1/token" and host == "token.agents.internal":
                    form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
                    gw.token_requests.append(form)
                    gw.issued += 1
                    self._reply(
                        200,
                        {
                            "access_token": agent_token(gw.issued),
                            "expires_in": 300,
                            "token_type": "Bearer",
                        },
                    )
                elif (
                    self.path == "/models/llm-main/v1/chat/completions"
                    and host == "inference.agents.internal"
                ):
                    gw.chat_requests.append(
                        {"auth": self.headers.get("Authorization"), "body": json.loads(body)}
                    )
                    status, reply = gw.chat_replies.pop(0)
                    self._reply(status, reply)
                else:
                    self._reply(404, {"error": "no route"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


AGENT_MODULE = '''
import json
from agent_runtime.tools import Tool

def create_agent(ctx):
    async def write_report(text):
        """Write the report into the workspace."""
        (ctx.workspace / "report.md").write_text(text)
        return {"written": len(text)}

    async def get_pod(name):
        """Get a pod."""
        (ctx.workspace / ("pod-" + name)).write_text("x")
        return {"pod": name}

    async def huge(kind):
        """Return an oversized result."""
        if kind == "string":
            return {"text": "a" * (10 * 1024 * 1024)}
        if kind == "list":
            return {"items": ["item-%d" % i for i in range(200000)]}
        if kind == "nested":
            v = "leaf"
            for _ in range(5000):
                v = [v]
            return {"v": v}
        return {"wide": {("k%d" % i) * 50: "\\u00e9" * 100 for i in range(5000)}}

    async def boom():
        """Raise."""
        raise KeyError("x")

    tools = [Tool(write_report, "workspace_write"), Tool(get_pod, "remote_read"),
             Tool(huge, "workspace_read"), Tool(boom, "workspace_read")]
    extra = (ctx.workspace.parent / "extra_tool")
    if extra.exists():
        async def surprise():
            """Not in the manifest."""
            return {}
        tools.append(Tool(surprise, "workspace_read"))
    return {"tools": tools}
'''

MANIFEST = {
    "schema_version": 1,
    "name": "conformance",
    "entrypoint": "fakeagent:create_agent",
    "instructions": "instructions/base.md",
    "model": {"backend": "llm-main", "name": "test-model", "max_tokens": 64, "temperature": 0},
    "tools": [
        {"name": "write_report", "effect": "workspace_write"},
        {"name": "get_pod", "effect": "remote_read"},
        {"name": "huge", "effect": "workspace_read"},
        {"name": "boom", "effect": "workspace_read"},
    ],
}


def make_app(directory: Path, manifest: dict = MANIFEST) -> Path:
    import yaml

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent.yaml").write_text(yaml.safe_dump(manifest))
    (directory / "instructions").mkdir(exist_ok=True)
    (directory / "instructions" / "base.md").write_text("You are a test agent.")
    (directory / "fakeagent.py").write_text(AGENT_MODULE)
    return directory
