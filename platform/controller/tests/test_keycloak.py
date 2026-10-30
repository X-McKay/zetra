"""KeycloakAdmin against an in-memory stand-in of the Keycloak 26 admin REST API."""

from __future__ import annotations

import json
import logging
import uuid

import httpx
import pytest

from registry_controller.idp import AgentClientSpec, IdpError, KeycloakAdmin

BASE = "https://idp.example.com"
ADMIN = "/admin/realms/agents"


class FakeKeycloak:
    def __init__(self) -> None:
        self.clients: dict[str, dict] = {}
        self.scopes: dict[str, dict] = {}  # id -> scope incl. protocolMappers
        self.client_scopes: dict[tuple[str, str], set[str]] = {}
        self.roles: dict[str, dict] = {}
        self.user_roles: dict[str, dict[str, dict]] = {}
        self.sa_user: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.token_requests = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/realms/agents/protocol/openid-connect/token":
            self.token_requests += 1
            return httpx.Response(200, json={"access_token": "opaque-admin", "expires_in": 300})
        assert request.headers["Authorization"] == "Bearer opaque-admin"
        assert path.startswith(ADMIN)
        sub = path[len(ADMIN) :]
        self.calls.append((method, sub))
        body = json.loads(request.content) if request.content else None
        parts = sub.strip("/").split("/")

        if parts == ["clients"] and method == "GET":
            cid = request.url.params.get("clientId")
            return httpx.Response(
                200, json=[c for c in self.clients.values() if c["clientId"] == cid]
            )
        if parts == ["clients"] and method == "POST":
            new = str(uuid.uuid4())
            self.clients[new] = dict(body, id=new)
            self.sa_user[new] = str(uuid.uuid4())
            self.user_roles[self.sa_user[new]] = {}
            return httpx.Response(201, headers={"Location": f"{BASE}{ADMIN}/clients/{new}"})
        if parts[0] == "clients" and len(parts) == 2:
            if method == "PUT":
                self.clients[parts[1]] = body
                return httpx.Response(204)
            if method == "DELETE":
                self.clients.pop(parts[1])
                return httpx.Response(204)
        if parts[0] == "clients" and parts[2] == "service-account-user":
            return httpx.Response(200, json={"id": self.sa_user[parts[1]]})
        if parts[0] == "clients" and parts[2] in (
            "default-client-scopes",
            "optional-client-scopes",
        ):
            key = (parts[1], parts[2])
            have = self.client_scopes.setdefault(key, set())
            if method == "GET":
                return httpx.Response(
                    200, json=[{"id": s, "name": self.scopes[s]["name"]} for s in sorted(have)]
                )
            (have.add if method == "PUT" else have.discard)(parts[3])
            return httpx.Response(204)
        if parts == ["client-scopes"] and method == "GET":
            return httpx.Response(
                200, json=[{"id": k, "name": v["name"]} for k, v in self.scopes.items()]
            )
        if parts == ["client-scopes"] and method == "POST":
            new = str(uuid.uuid4())
            mappers = [dict(m, id=str(uuid.uuid4())) for m in body.pop("protocolMappers", [])]
            self.scopes[new] = dict(body, id=new, protocolMappers=mappers)
            return httpx.Response(201, headers={"Location": f"{BASE}{ADMIN}/client-scopes/{new}"})
        if parts[0] == "client-scopes" and len(parts) == 2 and method == "DELETE":
            self.scopes.pop(parts[1])
            return httpx.Response(204)
        if parts[0] == "client-scopes" and parts[2] == "protocol-mappers":
            mappers = self.scopes[parts[1]]["protocolMappers"]
            if method == "GET":
                return httpx.Response(200, json=mappers)
            if method == "POST":
                mappers.append(dict(body, id=str(uuid.uuid4())))
                return httpx.Response(201, headers={"Location": "x/y"})
            if method == "PUT":
                mappers[:] = [body if m["id"] == parts[4] else m for m in mappers]
                return httpx.Response(204)
            mappers[:] = [m for m in mappers if m["id"] != parts[4]]
            return httpx.Response(204)
        if parts[0] == "roles":
            if method == "POST":
                self.roles[body["name"]] = dict(body, id=str(uuid.uuid4()))
                return httpx.Response(201)
            role = self.roles.get(parts[1])
            return httpx.Response(200, json=role) if role else httpx.Response(404)
        if parts[0] == "users" and parts[2] == "role-mappings":
            mapped = self.user_roles[parts[1]]
            if method == "GET":
                return httpx.Response(200, json=list(mapped.values()))
            for role in body:
                if method == "POST":
                    mapped[role["name"]] = role
                else:
                    mapped.pop(role["name"])
            return httpx.Response(204)
        return httpx.Response(500)


def spec(backends=("kubernetes",)) -> AgentClientSpec:
    return AgentClientSpec(
        client_id="agent-tenant-a-investigator",
        expected_subject="system:serviceaccount:tenant-a:agent-investigator",
        claims_scope="agent-claims-tenant-a-investigator",
        claims={
            "client_type": "agent",
            "agent_id": "tenant-a/investigator",
            "tenant": "tenant-a",
            "spiffe_id": "spiffe://cluster.local/ns/tenant-a/sa/agent-investigator",
        },
        mcp_scopes={f"mcp:{b}": f"https://mcp.example.com/{b}/mcp" for b in backends},
        realm_roles=frozenset(f"agent-role-{b}-read" for b in backends),
    )


@pytest.fixture
def kc():
    fake = FakeKeycloak()
    admin = KeycloakAdmin(
        BASE,
        "agents",
        "registry-controller",
        "not-a-real-secret",
        "kubernetes-cluster-a",
        http=httpx.Client(transport=httpx.MockTransport(fake)),
    )
    return fake, admin


def _scope(fake, name):
    return next(s for s in fake.scopes.values() if s["name"] == name)


def test_creates_client_per_7_3(kc):
    fake, admin = kc
    uid = admin.ensure_agent(spec())
    (client,) = fake.clients.values()
    assert client["clientId"] == "agent-tenant-a-investigator"
    assert client["publicClient"] is False and client["serviceAccountsEnabled"] is True
    assert not (
        client["standardFlowEnabled"]
        or client["implicitFlowEnabled"]
        or client["directAccessGrantsEnabled"]
    )
    assert client["clientAuthenticatorType"] == "federated-jwt"
    assert client["attributes"]["federated.jwt.identity.provider"] == "kubernetes-cluster-a"
    assert client["attributes"]["federated.jwt.expected.subject"] == (
        "system:serviceaccount:tenant-a:agent-investigator"
    )
    assert client["attributes"]["standard.token.exchange.enabled"] == "true"
    assert "secret" not in client
    assert uid == fake.sa_user[client["id"]]


def test_scopes_and_mappers_per_7_4(kc):
    fake, admin = kc
    admin.ensure_agent(spec())
    cid = next(iter(fake.clients))
    default = {fake.scopes[s]["name"] for s in fake.client_scopes[(cid, "default-client-scopes")]}
    optional = {fake.scopes[s]["name"] for s in fake.client_scopes[(cid, "optional-client-scopes")]}
    assert default == {"agent-gateway", "agent-claims-tenant-a-investigator"}
    assert optional == {"mcp:kubernetes"}
    gw = _scope(fake, "agent-gateway")["protocolMappers"]
    assert sorted(m["config"]["included.custom.audience"] for m in gw) == [
        "agent-gateway",
        "agent-policy-service",
    ]
    claims = {
        m["config"]["claim.name"]: m["config"]
        for m in _scope(fake, "agent-claims-tenant-a-investigator")["protocolMappers"]
    }
    assert {k: v["claim.value"] for k, v in claims.items()} == spec().claims
    assert all(
        v["access.token.claim"] == "true"
        and v["id.token.claim"] == "false"
        and v["jsonType.label"] == "String"
        for v in claims.values()
    )
    aud = _scope(fake, "mcp:kubernetes")["protocolMappers"][0]["config"]
    assert aud["included.custom.audience"] == "https://mcp.example.com/kubernetes/mcp"


def test_realm_roles_assigned_and_removed(kc):
    fake, admin = kc
    uid = admin.ensure_agent(spec(("kubernetes", "github")))
    assert set(fake.user_roles[uid]) == {"agent-role-kubernetes-read", "agent-role-github-read"}
    fake.user_roles[uid]["offline_access"] = {"name": "offline_access", "id": "x"}
    admin.ensure_agent(spec(("kubernetes",)))
    assert set(fake.user_roles[uid]) == {"agent-role-kubernetes-read", "offline_access"}
    cid = next(iter(fake.clients))
    optional = {fake.scopes[s]["name"] for s in fake.client_scopes[(cid, "optional-client-scopes")]}
    assert optional == {"mcp:kubernetes"}


def test_second_run_writes_nothing(kc):
    fake, admin = kc
    admin.ensure_agent(spec())
    fake.calls.clear()
    admin.ensure_agent(spec())
    assert [c for c in fake.calls if c[0] != "GET"] == []
    assert fake.token_requests == 1


def test_mapper_drift_is_corrected(kc):
    fake, admin = kc
    admin.ensure_agent(spec())
    scope = _scope(fake, "agent-claims-tenant-a-investigator")
    scope["protocolMappers"][0]["config"]["claim.value"] = "tampered"
    scope["protocolMappers"].append(
        {"id": "extra", "name": "extra", "protocolMapper": "x", "config": {}}
    )
    admin.ensure_agent(spec())
    values = {m["config"]["claim.value"] for m in scope["protocolMappers"]}
    assert values == set(spec().claims.values())


def test_delete(kc):
    fake, admin = kc
    admin.ensure_agent(spec())
    admin.delete_agent("agent-tenant-a-investigator", "agent-claims-tenant-a-investigator")
    assert fake.clients == {}
    assert all(s["name"] != "agent-claims-tenant-a-investigator" for s in fake.scopes.values())


def test_errors_raise_without_leaking_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    admin = KeycloakAdmin(
        BASE,
        "agents",
        "registry-controller",
        "not-a-real-secret",
        "kubernetes-cluster-a",
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
    )
    with pytest.raises(IdpError) as err:
        admin.ensure_agent(spec())
    assert "not-a-real-secret" not in str(err.value) + caplog.text
