"""Trusted credential-fixture checks for T5; never return bearer material."""

import base64
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def allowlisted_claims(claims: dict) -> dict:
    out = {}
    for key in ("iss", "sub", "jti", "client_type", "agent_id", "azp", "preferred_username"):
        value = claims.get(key)
        if isinstance(value, str) and len(value) <= 512:
            out[key] = value
    for key in ("iat", "exp", "nbf"):
        if type(claims.get(key)) is int:
            out[key] = claims[key]
    aud = claims.get("aud")
    if isinstance(aud, str) and len(aud) <= 512:
        out["aud"] = aud
    elif (
        isinstance(aud, list)
        and len(aud) <= 16
        and all(isinstance(a, str) and len(a) <= 512 for a in aud)
    ):
        out["aud"] = aud
    return out


def read_assertion(path: str) -> str:
    with Path(path).open("rb") as source:
        raw = source.read(65537)
    token = raw.decode("ascii").strip()
    if len(raw) > 65536 or not token or any(c.isspace() for c in token):
        raise ValueError("invalid assertion file")
    return token


def _unbase64(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JWT member")
    return result


def _decode(token: str) -> tuple[dict, dict, list[str]]:
    if len(token) > 65536:
        raise ValueError("token too large")
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid JWT")
    header = json.loads(_unbase64(parts[0]), object_pairs_hook=_object)
    claims = json.loads(_unbase64(parts[1]), object_pairs_hook=_object)
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise ValueError("invalid JWT objects")
    return header, claims, parts


def _current(claims: dict) -> bool:
    now = time.time()
    iat, exp, nbf = claims.get("iat"), claims.get("exp"), claims.get("nbf", 0)
    return (
        type(iat) is int
        and type(exp) is int
        and type(nbf) is int
        and iat <= now < exp
        and nbf <= now
        and iat < exp
    )


def verify_api_fixture(
    token: str, authenticated_as: str, expected_subject: str, issuer: str
) -> dict:
    """The API server's SelfSubjectReview is the signature/audience authority for this token."""
    _, claims, _ = _decode(token)
    parts = expected_subject.split(":")
    aud = claims.get("aud")
    aud = [aud] if isinstance(aud, str) else aud
    bound = claims.get("kubernetes.io", {})
    if not isinstance(bound, dict):
        raise ValueError("unbound API fixture")
    sa, pod = bound.get("serviceaccount", {}), bound.get("pod", {})
    if not (
        len(parts) == 4
        and parts[:2] == ["system", "serviceaccount"]
        and authenticated_as == expected_subject == claims.get("sub")
        and _current(claims)
        and isinstance(aud, list)
        and bool(aud)
        and all(isinstance(a, str) and a for a in aud)
        and not {issuer, "agent-policy-service", "agent-gateway"}.intersection(aud)
        and bound.get("namespace") == parts[2]
        and isinstance(sa, dict)
        and sa.get("name") == parts[3]
        and bool(sa.get("uid"))
        and isinstance(pod, dict)
        and bool(pod.get("name"))
        and bool(pod.get("uid"))
    ):
        raise ValueError("API fixture does not match authenticated bound serviceaccount")
    return allowlisted_claims(claims)


def api_assertion_identity(token: str) -> str:
    from kubernetes import client

    config = client.Configuration.get_default_copy()
    config.api_key = {"authorization": token}
    config.api_key_prefix = {"authorization": "Bearer"}
    config.refresh_api_key_hook = None  # authenticate this exact fixture, never the SDK default
    with client.ApiClient(config) as api:
        review = client.AuthenticationV1Api(api).create_self_subject_review(
            {"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"},
            _request_timeout=10,
        )
    return review.status.user_info.username


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_jwks(url: str) -> dict:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username:
        raise ValueError("invalid configured JWKS URL")
    with urllib.request.build_opener(_NoRedirect()).open(url, timeout=10) as response:
        data = response.read((1 << 20) + 1)
    if len(data) > 1 << 20:
        raise ValueError("JWKS too large")
    jwks = json.loads(data)
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise ValueError("invalid JWKS")
    return jwks


def verify_human_fixture(
    token: str, jwks: dict, issuer: str, subject: str, username: str, client_id: str
) -> dict:
    """A configured human identity, signed by the trusted issuer's RS256 key and still live."""
    header, claims, parts = _decode(token)
    if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
        raise ValueError("unsupported human token signature")
    keys = [
        k
        for k in jwks.get("keys", [])
        if isinstance(k, dict)
        and k.get("kid") == header["kid"]
        and k.get("kty") == "RSA"
        and k.get("alg", "RS256") == "RS256"
        and k.get("use", "sig") == "sig"
    ]
    if len(keys) != 1:
        raise ValueError("no unique human token signing key")
    try:
        key = rsa.RSAPublicNumbers(
            int.from_bytes(_unbase64(keys[0]["e"]), "big"),
            int.from_bytes(_unbase64(keys[0]["n"]), "big"),
        ).public_key()
        key.verify(
            _unbase64(parts[2]), ".".join(parts[:2]).encode(), padding.PKCS1v15(), hashes.SHA256()
        )
    except (InvalidSignature, KeyError, TypeError) as exc:
        raise ValueError("invalid human token signature") from exc
    if not (
        issuer.startswith("https://")
        and claims.get("iss") == issuer
        and _current(claims)
        and bool(subject)
        and claims.get("sub") == subject
        and not subject.startswith(("system:", "service-account-"))
        and bool(username)
        and not username.startswith("service-account-")
        and claims.get("preferred_username") == username
        and bool(client_id)
        and claims.get("azp") == client_id
        and claims.get("typ") == "Bearer"
        and claims.get("client_type") != "agent"
        and "agent_id" not in claims
        and "act" not in claims
    ):
        raise ValueError("human fixture identity or lifetime is invalid")
    return allowlisted_claims(claims)
