"""Session-scoped command authentication; transport does not choose progress."""

import hmac
import json
import time

from .records import canonical_bytes


class ProtocolError(ValueError):
    pass


class Authority:
    def __init__(self, key, session, *, generation):
        if len(key) < 32:
            raise ValueError("capability key too short")
        self.key = key
        self.session = session
        self.generation = generation

    def sign(self, action, payload, *, now=None, ttl=30):
        now = time.time() if now is None else now
        body = {
            "session": self.session,
            "generation": self.generation,
            "action": action,
            "expires": now + ttl,
            "payload": payload,
        }
        encoded = canonical_bytes(body)
        envelope = {
            "body": json.loads(encoded),
            "mac": hmac.digest(self.key, encoded, "sha256").hex(),
        }
        self.verify(envelope, now=now)
        return envelope

    def verify(self, envelope, *, now=None):
        now = time.time() if now is None else now
        try:
            if len(canonical_bytes(envelope)) > 262144:
                raise ProtocolError("command too large")
            body = envelope["body"]
            if set(envelope) != {"body", "mac"} or set(body) != {
                "session",
                "generation",
                "action",
                "expires",
                "payload",
            }:
                raise ProtocolError("invalid command schema")
            expected = hmac.digest(self.key, canonical_bytes(body), "sha256").hex()
            if not hmac.compare_digest(expected, envelope["mac"]):
                raise ProtocolError("invalid command MAC")
            if body["session"] != self.session or body["generation"] != self.generation:
                raise ProtocolError("stale or wrong-session command")
            if body["expires"] <= now or body["expires"] > now + 120:
                raise ProtocolError("expired or unbounded command")
            if body["action"] not in ("advance", "execute", "close"):
                raise ProtocolError("unknown command")
            return body["action"], json.loads(canonical_bytes(body["payload"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError(str(exc)) from exc


class HttpTransport:
    """Trusted local conformance transport; production reaches loopback via OpenShell exec."""

    def __init__(self, url, authority):
        self.url = url
        self.authority = authority

    async def call(self, action, payload):
        import httpx

        async with httpx.AsyncClient(timeout=35, trust_env=False) as client:
            response = await client.post(
                self.url, json=self.authority.sign(action, payload, ttl=40)
            )
            response.raise_for_status()
            if len(response.content) > 262144:
                raise ProtocolError("response too large")
            return response.json()


def enforce_payload_size(value, converter):
    """Measure actual default-codec Temporal protobuf payload bytes, including metadata."""
    size = sum(payload.ByteSize() for payload in converter.to_payloads([value]))
    if size > 262144:
        raise ProtocolError("converted Temporal payload exceeds 256 KiB")
    return value
