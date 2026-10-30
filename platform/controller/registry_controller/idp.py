"""Identity provider client management (6.4 steps 3-4, 7.3, 7.4) behind a small port."""

from __future__ import annotations

import copy
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from agent_platform.constants import AGENT_GATEWAY_SCOPE, AGENT_TOKEN_AUDIENCE, APS_AUDIENCE

AGENT_ROLE_RE = re.compile(r"^agent-role-[a-z0-9-]{1,63}-read$")


@dataclass(frozen=True)
class AgentClientSpec:
    client_id: str  # agent-<tenant>-<name>
    expected_subject: str  # system:serviceaccount:<tenant>:agent-<name>
    claims_scope: str  # agent-claims-<tenant>-<name>
    claims: dict[str, str]  # client_type, agent_id, tenant, spiffe_id
    mcp_scopes: dict[str, str] = field(default_factory=dict)  # mcp:<backend> -> resource URL
    realm_roles: frozenset[str] = frozenset()  # agent-role-<backend>-read


class IdpAdapter(Protocol):
    def ensure_agent(self, spec: AgentClientSpec) -> str:
        """Create or converge the client, scopes and roles; return the SA user's `sub`."""
        ...

    def delete_agent(self, client_id: str, claims_scope: str) -> None: ...


class IdpError(RuntimeError):
    pass


def _hardcoded(name: str, value: str) -> dict[str, Any]:
    return {
        "name": name,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-hardcoded-claim-mapper",
        "config": {
            "claim.name": name,
            "claim.value": value,
            "jsonType.label": "String",
            "access.token.claim": "true",
            "id.token.claim": "false",
            "userinfo.token.claim": "false",
            "introspection.token.claim": "true",
        },
    }


def _audience(name: str, audience: str) -> dict[str, Any]:
    return {
        "name": name,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.custom.audience": audience,
            "access.token.claim": "true",
            "id.token.claim": "false",
            "introspection.token.claim": "true",
        },
    }


def client_representation(
    spec: AgentClientSpec, idp_alias: str, template: dict[str, Any] | None
) -> dict[str, Any]:
    """7.3. With a template (the hand-made client exported by kcadm, 17.3) its attribute
    keys win; the per-agent values below are always set."""
    rep = copy.deepcopy(template or {})
    for key in ("id", "secret", "protocolMappers", "defaultClientScopes", "optionalClientScopes"):
        rep.pop(key, None)
    rep.update(
        clientId=spec.client_id,
        protocol="openid-connect",
        enabled=True,
        publicClient=False,
        bearerOnly=False,
        serviceAccountsEnabled=True,
        standardFlowEnabled=False,
        implicitFlowEnabled=False,
        directAccessGrantsEnabled=False,
        clientAuthenticatorType=rep.get("clientAuthenticatorType", "federated-jwt"),
    )
    attrs = rep.setdefault("attributes", {})
    attrs["federated.jwt.identity.provider"] = idp_alias
    attrs["federated.jwt.expected.subject"] = spec.expected_subject
    attrs["standard.token.exchange.enabled"] = "true"
    return rep


class KeycloakAdmin:
    """IdpAdapter over the Keycloak 26 admin REST API, as client `registry-controller`."""

    def __init__(
        self,
        base_url: str,
        realm: str,
        client_id: str,
        client_secret: str,
        idp_alias: str,
        template: dict[str, Any] | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._realm = realm
        self._client_id = client_id
        self._secret = client_secret
        self._alias = idp_alias
        self._template = template
        self._http = http or httpx.Client(timeout=5.0)
        self._token: tuple[str, float] | None = None

    # -- HTTP --------------------------------------------------------------------------

    def _bearer(self) -> str:
        if self._token and self._token[1] - 30 > time.monotonic():
            return self._token[0]
        r = self._http.post(
            f"{self._base}/realms/{self._realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._secret,
            },
        )
        if r.status_code != 200:
            raise IdpError(f"admin token request failed: HTTP {r.status_code}")
        body = r.json()
        self._token = (body["access_token"], time.monotonic() + float(body.get("expires_in", 60)))
        return self._token[0]

    def _call(self, method: str, path: str, *, ok=(200, 201, 204), **kw) -> httpx.Response:
        url = f"{self._base}/admin/realms/{self._realm}{path}"
        r = self._http.request(
            method, url, headers={"Authorization": f"Bearer {self._bearer()}"}, **kw
        )
        if r.status_code not in ok:
            raise IdpError(f"{method} {path}: HTTP {r.status_code}")
        return r

    def _created_id(self, r: httpx.Response) -> str:
        return r.headers["Location"].rstrip("/").rsplit("/", 1)[-1]

    # -- objects -----------------------------------------------------------------------

    def _find_client(self, client_id: str) -> dict | None:
        r = self._call("GET", "/clients", params={"clientId": client_id})
        return next((c for c in r.json() if c.get("clientId") == client_id), None)

    def _find_scope(self, name: str) -> dict | None:
        return next(
            (s for s in self._call("GET", "/client-scopes").json() if s["name"] == name), None
        )

    def _ensure_scope(self, name: str, mappers: list[dict[str, Any]]) -> str:
        scope = self._find_scope(name)
        if scope is None:
            r = self._call(
                "POST",
                "/client-scopes",
                json={
                    "name": name,
                    "protocol": "openid-connect",
                    "attributes": {"include.in.token.scope": "true"},
                    "protocolMappers": mappers,
                },
            )
            return self._created_id(r)
        sid = scope["id"]
        base = f"/client-scopes/{sid}/protocol-mappers/models"
        have = {m["name"]: m for m in self._call("GET", base).json()}
        want = {m["name"]: m for m in mappers}
        for mname, mapper in want.items():
            cur = have.get(mname)
            if cur is None:
                self._call("POST", base, json=mapper)
            elif (
                cur.get("protocolMapper") != mapper["protocolMapper"]
                or cur.get("config") != mapper["config"]
            ):
                self._call("PUT", f"{base}/{cur['id']}", json=dict(mapper, id=cur["id"]))
        for mname, cur in have.items():
            if mname not in want:
                self._call("DELETE", f"{base}/{cur['id']}")
        return sid

    def _ensure_client_scopes(self, cid: str, kind: str, want: dict[str, str]) -> None:
        """kind is default-client-scopes or optional-client-scopes; want maps name -> id."""
        have = {s["name"]: s["id"] for s in self._call("GET", f"/clients/{cid}/{kind}").json()}
        for name, sid in want.items():
            if name not in have:
                self._call("PUT", f"/clients/{cid}/{kind}/{sid}")
        if kind == "optional-client-scopes":
            for name, sid in have.items():
                if name.startswith("mcp:") and name not in want:
                    self._call("DELETE", f"/clients/{cid}/{kind}/{sid}")

    def _ensure_role(self, name: str) -> dict:
        r = self._call("GET", f"/roles/{quote(name)}", ok=(200, 404))
        if r.status_code == 404:
            self._call("POST", "/roles", json={"name": name, "description": "agent platform"})
            r = self._call("GET", f"/roles/{quote(name)}")
        return r.json()

    def ensure_agent(self, spec: AgentClientSpec) -> str:
        rep = client_representation(spec, self._alias, self._template)
        existing = self._find_client(spec.client_id)
        if existing is None:
            cid = self._created_id(self._call("POST", "/clients", json=rep))
        else:
            cid = existing["id"]
            merged = dict(existing, **{k: v for k, v in rep.items() if k != "attributes"})
            merged["attributes"] = dict(existing.get("attributes") or {}, **rep["attributes"])
            if merged != existing:
                self._call("PUT", f"/clients/{cid}", json=merged)

        gateway = self._ensure_scope(
            AGENT_GATEWAY_SCOPE,
            [
                _audience("aud-agent-gateway", AGENT_TOKEN_AUDIENCE),
                _audience("aud-agent-policy-service", APS_AUDIENCE),
            ],
        )
        claims = self._ensure_scope(
            spec.claims_scope, [_hardcoded(k, v) for k, v in sorted(spec.claims.items())]
        )
        optional = {
            name: self._ensure_scope(name, [_audience(f"aud-{name}", url)])
            for name, url in sorted(spec.mcp_scopes.items())
        }
        self._ensure_client_scopes(
            cid, "default-client-scopes", {AGENT_GATEWAY_SCOPE: gateway, spec.claims_scope: claims}
        )
        self._ensure_client_scopes(cid, "optional-client-scopes", optional)

        user = self._call("GET", f"/clients/{cid}/service-account-user").json()
        uid = user["id"]
        base = f"/users/{uid}/role-mappings/realm"
        have = {
            r["name"]: r for r in self._call("GET", base).json() if AGENT_ROLE_RE.match(r["name"])
        }
        add = [self._ensure_role(n) for n in sorted(spec.realm_roles - have.keys())]
        if add:
            self._call("POST", base, json=add)
        remove = [have[n] for n in sorted(have.keys() - spec.realm_roles)]
        if remove:
            self._call("DELETE", base, json=remove)
        return uid

    def delete_agent(self, client_id: str, claims_scope: str) -> None:
        existing = self._find_client(client_id)
        if existing is not None:
            self._call("DELETE", f"/clients/{existing['id']}", ok=(204, 404))
        scope = self._find_scope(claims_scope)
        if scope is not None:
            self._call("DELETE", f"/client-scopes/{scope['id']}", ok=(204, 404))
