"""Client assertion and agent access token cache (spec 4.1, 4.2, 11.2, 12.1).

Configuration is read from the environment when first used, never at import, so the step
protocol can reject bad input and tests can import the package without a pod environment.
"""

import base64
import json
import os
import pathlib
import ssl
import threading
import time

import httpx

REFRESH_BEFORE = 120  # seconds
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def gateway_url() -> str:
    url = env("AGENT_GATEWAY_URL").rstrip("/")  # https://10.43.250.10:443
    if not url.startswith("https://"):
        raise RuntimeError("AGENT_GATEWAY_URL must be https: bearer tokens never travel in clear")
    return url


def tls_verify() -> ssl.SSLContext:
    """The internal CA when AGENT_GATEWAY_CA is set, else the system store. Never disabled."""
    ca = os.environ.get("AGENT_GATEWAY_CA")
    return ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()


def sandbox_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("AGENT_SANDBOX_DIR", "/sandbox"))


def state_dir() -> pathlib.Path:
    return sandbox_dir() / "state"


def token_cache() -> pathlib.Path:
    return state_dir() / "token.json"


def _claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise RuntimeError("issued token is not a JWT") from None


def write_private(path: pathlib.Path, data: str) -> None:
    """Atomically replace `path` with `data`; the file is 0600 from creation, before any byte."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class AgentIdentity:
    """Holds the current agent access token; thread safe; refreshes before expiry; persists
    across boundary processes in token.json (0600)."""

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._exp = 0.0
        self._transport = transport
        self._http: httpx.Client | None = None

    def token(self) -> str:
        with self._lock:
            if self._token is None:
                self._load()
            if self._token is None or time.time() > self._exp - REFRESH_BEFORE:
                self._refresh()
            return self._token

    def invalidate(self, rejected: str | None = None) -> None:
        """Forget the token. With `rejected`, only if it is still the current one."""
        with self._lock:
            if rejected is not None and rejected != self._token:
                return
            self._token = None
            token_cache().unlink(missing_ok=True)

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=gateway_url(),
                verify=tls_verify(),
                timeout=10.0,
                follow_redirects=False,
                transport=self._transport,
            )
        return self._http

    def _load(self) -> None:
        try:
            d = json.loads(token_cache().read_text(encoding="utf-8"))
            self._token, self._exp = str(d["access_token"]), float(d["exp"])
        except (OSError, ValueError, KeyError, TypeError):
            self._token = None

    def _refresh(self) -> None:
        # Re-read on every refresh: the kubelet rotates the projected token (4.1).
        with open(env("AGENT_TOKEN_PATH"), encoding="utf-8") as f:
            assertion = f.read().strip()
        started = time.time()
        r = self._client().post(
            env("AGENT_TOKEN_ENDPOINT_PATH"),
            headers={"Host": env("AGENT_TOKEN_HOST")},
            data={
                "grant_type": "client_credentials",
                "client_id": env("AGENT_CLIENT_ID"),
                "client_assertion_type": ASSERTION_TYPE,
                "client_assertion": assertion,
                "scope": "agent-gateway",
            },
        )
        if r.status_code != 200:
            try:
                error = str(r.json().get("error", "?"))[:64]
            except (ValueError, AttributeError):
                error = "?"
            raise RuntimeError(f"token endpoint returned {r.status_code}: {error}")
        body = r.json()
        token = body["access_token"]
        claims = _claims(token)
        aud = claims.get("aud")
        if "agent-gateway" not in (aud if isinstance(aud, list) else [aud]):
            raise RuntimeError("issued token is not for the gateway")
        # Expiry is measured from before the request, and never later than the token's own exp.
        exp = started + int(body.get("expires_in", 300))
        if isinstance(claims.get("exp"), int | float):
            exp = min(exp, float(claims["exp"]))
        write_private(token_cache(), json.dumps({"access_token": token, "exp": exp}))
        self._token, self._exp = token, exp


class BearerAuth(httpx.Auth):
    """Sets Authorization on every request; on one 401 refreshes and retries once.
    A 403 is never retried. The last status is kept so callers can report 403 as a policy
    error and 429 as a budget error."""

    requires_request_body = True

    def __init__(self, identity: "AgentIdentity") -> None:
        self.identity = identity
        self.status: int | None = None

    def auth_flow(self, request: httpx.Request):
        token = self.identity.token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            self.identity.invalidate(rejected=token)
            request.headers["Authorization"] = f"Bearer {self.identity.token()}"
            response = yield request
        self.status = response.status_code


_IDENTITY: AgentIdentity | None = None
_IDENTITY_LOCK = threading.Lock()


def identity() -> AgentIdentity:
    """The process-wide identity, created on first use."""
    global _IDENTITY
    with _IDENTITY_LOCK:
        if _IDENTITY is None:
            _IDENTITY = AgentIdentity()
        return _IDENTITY
