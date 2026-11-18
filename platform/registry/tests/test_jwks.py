"""`JwksVerifier` against keys made here. Nothing touches the network."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from agent_registry.api.app import create_app
from agent_registry.api.auth import AuthError
from agent_registry.api.jwks import JwksVerifier
from agent_registry.config import Settings
from agent_registry.store.memory import MemoryStore

SETTINGS = Settings()
KID = "key-1"
MISSING = object()


def make_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(private, kid: str) -> dict:
    document = jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    return {**document, "kid": kid, "alg": "RS256", "use": "sig"}


def claims(**changes) -> dict:
    now = int(time.time())
    base = {
        "sub": "u-1",
        "iss": SETTINGS.issuer,
        "aud": ["account", SETTINGS.audience],
        "exp": now + 300,
        "iat": now,
        "typ": "Bearer",
        "preferred_username": "al",
        "email": "al@example.com",
        "realm_access": {"roles": ["registry-operator", "other"]},
    }
    base.update(changes)
    return {k: v for k, v in base.items() if v is not MISSING}


def sign(private, kid: str = KID, **changes) -> str:
    return jwt.encode(claims(**changes), private, algorithm="RS256", headers={"kid": kid})


class Realm:
    """A JWKS endpoint that counts how often it is asked, and a clock that moves when told."""

    def __init__(self, *keys: tuple[str, object]):
        self.keys = dict(keys)
        self.fetches = 0
        self.broken = False
        self.time = 1000.0

    def fetch(self) -> dict:
        self.fetches += 1
        if self.broken:
            raise OSError("connection refused to https://idp.internal/realms/agents")
        return {"keys": [jwk(key, kid) for kid, key in self.keys.items()]}

    def verifier(self) -> JwksVerifier:
        return JwksVerifier(SETTINGS, fetch=self.fetch, clock=lambda: self.time)


@pytest.fixture(scope="module")
def key():
    return make_key()


@pytest.fixture(scope="module")
def other_key():
    return make_key()


def test_a_person_with_an_email(key):
    verifier = Realm((KID, key)).verifier()

    principal = verifier.verify(sign(key))

    assert principal.subject == "u-1"
    assert principal.username == "al"
    assert principal.roles == {"registry-operator", "other"}
    assert (principal.person, principal.email, principal.cluster) == (True, "al@example.com", None)


def test_a_service_account_is_not_a_person_and_has_no_email(key):
    verifier = Realm((KID, key)).verifier()

    token = sign(
        key,
        client_id="ci",
        preferred_username="service-account-ci",
        azp="ci",
        realm_access={"roles": ["registry-reader"]},
    )
    principal = verifier.verify(token)

    assert (principal.person, principal.email, principal.cluster) == (False, None, None)
    assert principal.roles == {"registry-reader"}


def test_a_provisioner_is_known_by_the_cluster_in_its_client_name(key):
    verifier = Realm((KID, key)).verifier()

    token = sign(
        key,
        preferred_username="service-account-registry-provisioner-kubani",
        azp="registry-provisioner-kubani",
        realm_access={"roles": ["registry-provisioner"]},
    )
    principal = verifier.verify(token)

    assert (principal.person, principal.cluster) == (False, "kubani")


def test_a_client_token_as_keycloak_issues_it_carries_client_id_and_no_username(key):
    # What realm `agents` gives the registry's own clients: their scopes are basic, roles and
    # agent-registry, so there is no preferred_username; the scope's mapper adds client_id.
    verifier = Realm((KID, key)).verifier()

    token = sign(
        key,
        sub="5d0e5e0e-0000-4000-8000-000000000001",
        client_id="registry-provisioner-kubani",
        azp="registry-provisioner-kubani",
        preferred_username=MISSING,
        email=MISSING,
        realm_access={"roles": ["registry-provisioner"]},
    )
    principal = verifier.verify(token)

    assert (principal.person, principal.cluster) == (False, "kubani")
    assert principal.actor == "registry-provisioner-kubani"


def test_a_client_token_with_neither_claim_is_not_taken_for_a_provisioner(key):
    verifier = Realm((KID, key)).verifier()

    token = sign(
        key,
        azp="registry-provisioner-kubani",
        preferred_username=MISSING,
        email=MISSING,
        realm_access={"roles": ["registry-provisioner"]},
    )
    principal = verifier.verify(token)

    assert principal.cluster is None


def forged_hs256(secret: bytes) -> str:
    def part(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    signing_input = f"{part({'alg': 'HS256', 'typ': 'JWT', 'kid': KID})}.{part(claims())}"
    mac = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def refused_tokens(key, other_key) -> dict[str, str]:
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return {
        "expired": sign(key, exp=int(time.time()) - 60),
        "issued in the future": sign(key, iat=int(time.time()) + 600),
        "wrong audience": sign(key, aud="another-service"),
        "wrong issuer": sign(key, iss="https://idp.example.com/realms/other"),
        "another key under the same kid": sign(other_key),
        "alg none": jwt.encode(claims(), None, algorithm="none", headers={"kid": KID}),
        "hs256 with the public key as secret": forged_hs256(public_pem),
        "id token": sign(key, typ="ID"),
        "refresh token": sign(key, typ="Refresh"),
        "no sub": sign(key, sub=MISSING),
        "no exp": sign(key, exp=MISSING),
        "no audience": sign(key, aud=MISSING),
        "garbage": "not.a.token",
        "empty": "",
    }


def test_a_bad_token_is_refused_and_never_quoted(key, other_key):
    verifier = Realm((KID, key)).verifier()

    for why, token in refused_tokens(key, other_key).items():
        with pytest.raises(AuthError) as refused:
            verifier.verify(token)
        message = str(refused.value)
        assert message, why
        for piece in (p for p in token.split(".") if len(p) > 16):
            assert piece not in message, why


def test_a_key_the_realm_starts_using_is_fetched_once_and_no_more_than_every_30_seconds(
    key, other_key
):
    realm = Realm((KID, key))
    verifier = realm.verifier()
    verifier.verify(sign(key))
    verifier.verify(sign(key))
    assert realm.fetches == 1  # the second token used the cache

    realm.time += 100
    realm.keys = {"key-2": other_key}  # the realm rotates
    assert verifier.verify(sign(other_key, kid="key-2")).subject == "u-1"
    assert realm.fetches == 2

    realm.time += 5
    for kid in ("key-3", "key-4", "key-3"):
        with pytest.raises(AuthError):
            verifier.verify(sign(other_key, kid=kid))
    assert realm.fetches == 2  # unknown kids inside 30 seconds do not reach the realm

    realm.time += 30
    with pytest.raises(AuthError):
        verifier.verify(sign(other_key, kid="key-3"))
    assert realm.fetches == 3


def test_keys_that_cannot_be_fetched_refuse_an_unknown_kid_and_keep_the_known_ones(key):
    realm = Realm((KID, key))
    verifier = realm.verifier()
    realm.broken = True

    with pytest.raises(AuthError) as refused:
        verifier.verify(sign(key))
    assert "idp.internal" not in str(refused.value)

    realm.broken = False
    realm.time += 31
    verifier.verify(sign(key))
    realm.broken = True
    realm.time += 31
    verifier.verify(sign(key))  # cached: the realm is not asked
    assert realm.fetches == 2


def test_the_application_wants_a_good_token_for_this_audience(key):
    verifier = Realm((KID, key)).verifier()
    client = TestClient(create_app(MemoryStore(), SETTINGS, verifier))
    reader = {"realm_access": {"roles": ["registry-reader"]}}

    def get(token: str | None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return client.get("/v1/meta", headers=headers)

    for answer in (
        get(None),
        get(sign(key, exp=int(time.time()) - 60, **reader)),
        get(sign(key, aud="another-service", **reader)),
    ):
        assert answer.status_code == 401
        assert answer.json()["error"]["code"] == "not-signed-in"
    assert get(sign(key, **reader)).status_code == 200
