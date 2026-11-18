"""`GithubJwksVerifier` against keys made here, and the two kinds of token kept apart."""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient

from agent_registry.api.app import create_app
from agent_registry.api.auth import AuthError
from agent_registry.api.github import GithubJwksVerifier
from agent_registry.api.jwks import JwksVerifier
from agent_registry.config import Settings
from agent_registry.store.memory import MemoryStore
from tests.test_jwks import KID, jwk, make_key
from tests.test_jwks import sign as realm_sign

SETTINGS = Settings(report_audience="agent-registry-reports")
WORKFLOW = "X-McKay/zetra-agents/.github/workflows/release.yml@refs/tags/db-admin/v0.2.1"


@pytest.fixture(scope="module")
def github_key():
    return make_key()


@pytest.fixture(scope="module")
def realm_key():
    return make_key()


def github_token(key, **changes) -> str:
    now = int(time.time())
    claims = {
        "iss": SETTINGS.github_issuer,
        "aud": SETTINGS.report_audience,
        "sub": "repo:X-McKay/zetra-agents:ref:refs/tags/db-admin/v0.2.1",
        "exp": now + 300,
        "iat": now,
        "repository": "X-McKay/zetra-agents",
        "ref": "refs/tags/db-admin/v0.2.1",
        "ref_type": "tag",
        "sha": "a" * 40,
        "job_workflow_ref": WORKFLOW,
        **changes,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


def verifier(key) -> GithubJwksVerifier:
    return GithubJwksVerifier(SETTINGS, fetch=lambda: {"keys": [jwk(key, KID)]})


def test_the_claims_a_report_needs_come_from_a_good_token(github_key):
    claims = verifier(github_key).verify(github_token(github_key))

    assert (claims.repository, claims.ref, claims.ref_type) == (
        "X-McKay/zetra-agents",
        "refs/tags/db-admin/v0.2.1",
        "tag",
    )
    assert (claims.sha, claims.job_workflow_ref) == ("a" * 40, WORKFLOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"aud": "something-else"},
        {"iss": "https://idp.almckay.io/realms/agents"},
        {"exp": int(time.time()) - 3600},
        {"job_workflow_ref": None},
    ],
)
def test_a_token_that_is_not_for_this_service_is_refused(github_key, changes):
    with pytest.raises(AuthError):
        verifier(github_key).verify(github_token(github_key, **changes))


def test_a_token_signed_by_another_key_is_refused(github_key, realm_key):
    with pytest.raises(AuthError):
        verifier(github_key).verify(github_token(realm_key))


def test_a_token_for_one_kind_of_route_is_refused_on_the_other(github_key, realm_key):
    # Each verifier checks against its own issuer's keys, so neither takes the other's token.
    app = create_app(
        MemoryStore(),
        SETTINGS,
        JwksVerifier(SETTINGS, fetch=lambda: {"keys": [jwk(realm_key, KID)]}),
        github_verifier=verifier(github_key),
    )
    client = TestClient(app)
    mine = {"Authorization": f"Bearer {github_token(github_key)}"}
    theirs = {"Authorization": f"Bearer {realm_sign(realm_key)}"}

    on_report = client.post("/v1/reports/releases", json={}, headers=theirs)
    on_read = client.get("/v1/deployments", headers=mine)

    assert on_report.status_code == 401
    assert on_report.json()["error"]["code"] == "not-signed-in"
    assert on_read.status_code == 401
    assert on_read.json()["error"]["code"] == "not-signed-in"
