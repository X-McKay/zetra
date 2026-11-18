"""Delivery sections 2 and 8.3: the carried declaration, and cancelling a revoked release's
sessions after its AgentRelease is written."""

import copy
import logging

from registry_provisioner.main import main
from registry_provisioner.registry import RegistryError
from tests.fakes import AV, FakeSessions

NS = "tenant-test"
RELEASE = "db-admin-2026-11-18-aaaaaaaa"
REVOCATION = {
    "id": "rev-1",
    "tenant": NS,
    "namespace": NS,
    "agent": "db-admin",
    "release": RELEASE,
    "sessions": "cancel",
}
REASON = "release revoked: rev-1"


def run(kube, registry, env, sessions):
    return main(["--once"], kube=kube, registry=registry, sessions=sessions, environ=env)


def revoked(desired):
    desired = copy.deepcopy(desired)
    desired["revocations"] = [dict(REVOCATION)]
    return desired


def writes(kube):
    return [e for e in kube.log if e[0] == "write" and e[1] == "AgentRelease"]


def test_declaration_reaches_the_cluster_unchanged(kube, registry, desired, env):
    declaration = {"description": "carried", "capabilities": {"models": []}}
    for obj in desired["deployments"][0]["objects"]:
        if obj["name"] == RELEASE:
            obj["document"]["spec"]["declaration"] = copy.deepcopy(declaration)
    registry.answer = desired
    run(kube, registry, env, None)
    assert kube.obj(AV, "AgentRelease", NS, RELEASE)["spec"]["declaration"] == declaration


def test_release_is_disabled_before_its_sessions_are_cancelled(kube, registry, desired, env):
    registry.answer = revoked(desired)
    sessions = FakeSessions(kube)
    run(kube, registry, env, sessions)
    order = [e for e in kube.log if e[0] == "cancel" or (e[0] == "write" and e[2] == RELEASE)]
    assert order == [("write", "AgentRelease", RELEASE, {"approved": False}), ("cancel", RELEASE)]


def test_no_cancel_when_the_write_fails_and_one_when_it_succeeds(kube, registry, desired, env):
    registry.answer = revoked(desired)
    sessions = FakeSessions(kube)
    kube.fail_apply = {RELEASE}
    assert run(kube, registry, env, sessions) == 0
    assert sessions.calls == []
    kube.fail_apply = set()
    run(kube, registry, env, sessions)
    assert len(sessions.calls) == 1


def test_failed_cancel_is_tried_again_and_does_not_fail_the_pass(
    kube, registry, desired, env, caplog
):
    registry.answer = revoked(desired)
    sessions = FakeSessions(kube)
    sessions.error = RegistryError("cancel by release answered 503")
    caplog.set_level(logging.INFO, logger="registry_provisioner")
    assert run(kube, registry, env, sessions) == 0
    assert len(sessions.calls) == 1
    assert "rev-1" in caplog.text and "will be tried again" in caplog.text
    sessions.error = None
    run(kube, registry, env, sessions)
    assert len(sessions.calls) == 2
    assert "cancelled 2 session(s)" in caplog.text


def test_session_interface_is_not_called_without_revocations_or_a_setting(
    kube, registry, desired, env, caplog
):
    sessions = FakeSessions(kube)
    run(kube, registry, env, sessions)  # no revocations in the answer
    assert sessions.calls == []
    registry.answer = revoked(desired)
    with caplog.at_level(logging.INFO, logger="registry_provisioner"):
        run(kube, registry, env, None)  # the setting is unset
    assert "not cancelled: no session interface configured" in caplog.text
    assert sessions.calls == []


def test_cancel_carries_agent_release_reason_to_the_tenants_url(kube, registry, desired, env):
    import httpx

    from registry_provisioner.registry import HttpRegistry
    from registry_provisioner.sessions import HttpSessions

    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"cancelled": ["s1", "s2"], "failed": []})

    class Tokens:
        def token(self):
            return "t"

    http = httpx.Client(transport=httpx.MockTransport(handler))
    registry_ = HttpRegistry("https://registry.test", "kubani", Tokens(), http)
    sessions = HttpSessions("http://session-api-{tenant}.agent-platform.svc:8080", registry_)
    assert sessions.cancel_release(NS, "db-admin", RELEASE, REASON) == 2
    (request,) = seen
    assert str(request.url) == (
        "http://session-api-tenant-test.agent-platform.svc:8080/v1/tenants/tenant-test/sessions/cancel"
    )
    assert request.headers["authorization"] == "Bearer t"
    assert request.read() == (
        b'{"agent":"db-admin","release":"%s","reason":"%s"}' % (RELEASE.encode(), REASON.encode())
    )
    # and the pass passes the same three things on
    registry.answer = revoked(desired)
    fake = FakeSessions(kube)
    run(kube, registry, env, fake)
    assert fake.calls == [(NS, "db-admin", RELEASE, REASON)]
