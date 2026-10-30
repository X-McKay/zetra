"""Agent Policy Service client (9.7), authenticated with the runner's projected token."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from temporalio.exceptions import ApplicationError

DEFAULT_URL = "http://agent-policy-service.agent-platform.svc.cluster.local:9002"
# Projected ServiceAccount token, audience agent-policy-service, rewritten by the kubelet.
DEFAULT_TOKEN_PATH = "/var/run/secrets/agent-policy-service/token"


class APS:
    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        token_path: str = DEFAULT_TOKEN_PATH,
        timeout: float = 10.0,
        ca_file: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token_path = Path(token_path)
        self.timeout = timeout
        self.context = ssl.create_default_context(cafile=ca_file) if ca_file else None

    @classmethod
    def from_env(cls) -> APS:
        return cls(
            os.environ.get("APS_URL", DEFAULT_URL),
            os.environ.get("APS_TOKEN_PATH", DEFAULT_TOKEN_PATH),
            ca_file=os.environ.get("APS_CA_FILE") or None,
        )

    def get(self, path: str) -> Any:
        return self._call("GET", path)

    def put(self, path: str, body: dict) -> Any:
        return self._call("PUT", path, body)

    def post(self, path: str, body: dict) -> Any:
        return self._call("POST", path, body)

    def delete(self, path: str) -> Any:
        return self._call("DELETE", path, missing_ok=True)

    def _call(self, method: str, path: str, body: dict | None = None, missing_ok: bool = False):
        # Re-read on every call: the kubelet rotates the file at 80 percent of its lifetime.
        token = self.token_path.read_text().strip()
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.context) as resp:
                raw = resp.read(1 << 20)
        except urllib.error.HTTPError as e:
            status = e.code
            e.close()
            if missing_ok and status == 404:
                return None
            if 400 <= status < 500:
                raise ApplicationError(
                    f"APS {method} refused with {status}", type="Forbidden", non_retryable=True
                ) from None
            raise ApplicationError(f"APS {method} failed with {status}", type="APSUnavailable")
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            # The message never includes the request, which carries the token.
            raise ApplicationError(
                f"APS unreachable: {type(e).__name__}", type="APSUnavailable"
            ) from None
        return json.loads(raw) if raw.strip() else None
