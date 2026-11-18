from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from session_api.app import create_app
from session_api.auth import Principal, StaticVerifier
from session_api.config import Settings
from tests import fakes

TENANT = "tenant-test"
CLUSTER = "kubani"


def person(*roles, email="al@example.com"):
    return Principal("s1", "al", frozenset(roles), True, email)


def service(azp, *roles):
    return Principal("s2", f"service-account-{azp}", frozenset(roles), False, None, azp)


TOKENS = {
    "operator": person("registry-operator"),
    "reader": person("registry-reader"),
    "evaluator": service(f"registry-evaluator-{CLUSTER}", "registry-evaluator"),
    "other-evaluator": service("registry-evaluator-elsewhere", "registry-evaluator"),
    "provisioner": service(f"registry-provisioner-{CLUSTER}", "registry-provisioner"),
    "other-provisioner": service("registry-provisioner-elsewhere", "registry-provisioner"),
}


class World:
    def __init__(self) -> None:
        self.sessions = fakes.FakeSessions()
        self.objects = fakes.FakeObjects()
        self.reports = fakes.FakeReports()
        self.objects.agents["db-admin"] = fakes.agent()
        self.objects.releases["db-admin-r1"] = fakes.release()
        settings = Settings(tenant=TENANT, cluster=CLUSTER, artifact_root="/unused")
        app = create_app(
            settings,
            StaticVerifier(TOKENS),
            self.sessions,
            self.objects,
            self.reports,
            clock=lambda: fakes.T0,
        )
        self.http = TestClient(app)

    def call(self, method, path, who="operator", json=None):
        headers = {"Authorization": f"Bearer {who}"} if who else {}
        return self.http.request(method, f"/v1/tenants/{TENANT}{path}", headers=headers, json=json)

    def start(self, who="operator", **body):
        body = {"agent": "db-admin", "task": {"prompt": "go"}} | body
        return self.call("POST", "/sessions", who, body)


@pytest.fixture
def world():
    return World()
