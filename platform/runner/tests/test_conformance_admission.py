"""Admission evidence must distinguish Kyverno enforcement from unrelated API failures."""

from __future__ import annotations

import copy
import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from kubernetes.client.exceptions import ApiException
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent

from runner import conformance_admission as a
from runner.types import SessionResult


def pod():
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "template",
            "namespace": "tenant-test",
            "labels": {
                "agentplatform.io/sandbox": "true",
                "agentplatform.io/agent": "conformance-b",
                "agentplatform.io/release": "signed",
                "agentplatform.io/session": "placeholder",
                "agentplatform.io/generation": "0",
            },
            "annotations": {"agentplatform.io/policy-digest": "sha256:abc"},
        },
        "spec": {
            "runtimeClassName": "kata-qemu-runtime-rs",
            "automountServiceAccountToken": False,
            "serviceAccountName": "agent-conformance-b",
            "initContainers": [{"name": "egress-guard", "image": "registry/guard@sha256:1"}],
            "containers": [{"name": "agent", "image": "registry/agent@sha256:2"}],
            "volumes": [{"name": "workspace", "emptyDir": {}}],
        },
    }


def rejection(policy="agent-sandbox-admission", rule="sandbox-shape", status=400):
    err = ApiException(status=status, reason="Bad Request")
    err.body = f'admission webhook "validate.kyverno.svc-fail" denied the request:\n\n{policy}:\n  {rule}: forbidden'
    return err


@pytest.mark.parametrize("status", [400, 403, 422])
def test_named_kyverno_denial_is_evidence(status):
    result = a.admission_result(
        rejection(status=status), "agent-sandbox-admission", "sandbox-shape"
    )
    assert result["result"] == "pass" and result["checks"]["expected_rule"]


@pytest.mark.parametrize(
    "error",
    [
        ApiException(status=403, reason="RBAC forbidden"),
        ApiException(status=422, reason="invalid pod field"),
        rejection(rule="release-must-be-ready-and-match"),
        rejection(status=500),
    ],
)
def test_unrelated_refusal_never_counts_as_enforcement(error):
    assert a.admission_result(error, "agent-sandbox-admission", "sandbox-shape")["result"] != "pass"


def test_unexpected_admission_fails_and_denied_positive_control_errors():
    assert a.admission_result(None, "agent-sandbox-admission", "sandbox-shape")["result"] == "fail"
    assert a.admission_result(None)["result"] == "pass"
    assert a.admission_result(rejection())["result"] == "error"


def test_rule_must_belong_to_expected_policy_and_kubernetes_json_is_decoded():
    error = rejection(rule="sandbox-no-extras")
    error.body += "\nother-policy:\n  sandbox-shape: denied"
    assert (
        a.admission_result(error, "agent-sandbox-admission", "sandbox-shape")["result"] == "error"
    )
    error = rejection()
    error.body = json.dumps({"kind": "Status", "message": error.body})
    assert a.admission_result(error, "agent-sandbox-admission", "sandbox-shape")["result"] == "pass"


def test_t22_cases_are_independent_and_cover_every_shape():
    original = pod()
    cases = {c.name: c for c in a.shape_cases(original)}
    assert set(cases) == {
        "extra_container",
        "changed_init_image",
        "hostpath_volume",
        "wrong_serviceaccount",
        "no_sandbox_label",
        "automount_true",
        "automount_omitted",
        "runtimeclass_omitted",
        "hostnetwork_true",
    }
    assert len(cases["extra_container"].pod["spec"]["containers"]) == 2
    assert (
        cases["changed_init_image"].pod["spec"]["initContainers"][0]["image"]
        != original["spec"]["initContainers"][0]["image"]
    )
    assert cases["hostpath_volume"].pod["spec"]["volumes"][-1]["hostPath"] == {"path": "/"}
    assert cases["wrong_serviceaccount"].pod["spec"]["serviceAccountName"] == "default"
    assert "agentplatform.io/sandbox" not in cases["no_sandbox_label"].pod["metadata"]["labels"]
    assert cases["automount_true"].pod["spec"]["automountServiceAccountToken"] is True
    assert "automountServiceAccountToken" not in cases["automount_omitted"].pod["spec"]
    assert "runtimeClassName" not in cases["runtimeclass_omitted"].pod["spec"]
    assert cases["hostnetwork_true"].pod["spec"]["hostNetwork"] is True
    assert original == pod()
    assert len({c.pod["metadata"]["name"] for c in cases.values()}) == 9


def test_extra_container_keeps_the_valid_container_security_and_shape():
    original = pod()
    original["spec"]["containers"][0].update(
        securityContext={
            "allowPrivilegeEscalation": False,
            "runAsNonRoot": True,
            "capabilities": {"drop": ["ALL"]},
            "readOnlyRootFilesystem": True,
        },
        command=["/app/.venv/bin/python", "-m", "agent_runtime.wait"],
        resources={"requests": {"cpu": "1", "memory": "1Gi"}},
    )
    extra = next(c for c in a.shape_cases(original) if c.name == "extra_container")
    assert extra.pod["spec"]["containers"][1] == {
        **original["spec"]["containers"][0],
        "name": "extra",
    }
    extra.pod["spec"]["containers"][1]["securityContext"]["allowPrivilegeEscalation"] = True
    assert original["spec"]["containers"][0]["securityContext"]["allowPrivilegeEscalation"] is False


# Exact Kubernetes messages retained from live Phase B run15 (2026-11-06).
PSA_HOSTPATH = 'pods "admission-35528428-c99d-4a1b-9947-b37363b91ae7" is forbidden: violates PodSecurity "restricted:v1.34": restricted volume types (volume "forbidden" uses restricted volume type "hostPath")'
PSA_HOSTNETWORK = 'pods "admission-982d60fd-0005-422c-a799-8adb214f7f6e" is forbidden: violates PodSecurity "restricted:v1.34": host namespaces (hostNetwork=true)'
PSA_EXTRA = 'pods "admission-c4e3b85d-1fc5-4bcc-a057-56c3a5da830e" is forbidden: violates PodSecurity "restricted:v1.34": allowPrivilegeEscalation != false (container "extra" must set securityContext.allowPrivilegeEscalation=false), unrestricted capabilities (container "extra" must set securityContext.capabilities.drop=["ALL"])'


def psa_error(message, status=403):
    error = ApiException(status=status, reason="Forbidden")
    error.body = json.dumps({"kind": "Status", "message": message})
    return error


@pytest.mark.parametrize(
    "case,message", [("hostpath_volume", PSA_HOSTPATH), ("hostnetwork_true", PSA_HOSTNETWORK)]
)
def test_t22_accepts_exact_restricted_psa_violation_and_exposes_its_layer(case, message):
    class Core:
        def create_namespaced_pod(self, namespace, body, **kwargs):
            assert kwargs["dry_run"] == "All"
            raise psa_error(message)

    candidate = next(c for c in a.shape_cases(pod()) if c.name == case)
    result = a.KubernetesAdmission(Core(), None, "tenant-test").admit(
        candidate.pod, candidate.policy, candidate.rule, psa_violation=candidate.psa_violation
    )
    assert result["result"] == "pass" and all(result["checks"].values())
    assert result["evidence"]["admission_layer"] == "pod-security-admission"
    assert result["evidence"]["pod_security_policy"] == "restricted:v1.34"
    assert result["evidence"]["message"] == message
    # T9b's same API error still requires the exact named Kyverno rule.
    assert a.admission_result(psa_error(message), a.POLICY, a.RELEASE_RULE)["result"] == "error"


@pytest.mark.parametrize(
    "message,status,expected",
    [
        (PSA_HOSTPATH, 403, "hostNetwork"),
        (PSA_HOSTNETWORK, 403, "hostPath"),
        (PSA_EXTRA, 403, "hostPath"),
        (PSA_HOSTPATH.replace("restricted:v1.34", "baseline:v1.34"), 403, "hostPath"),
        (PSA_HOSTPATH, 500, "hostPath"),
        (
            'pods "admission-test" is forbidden: User "harness" cannot create resource "pods"',
            403,
            "hostPath",
        ),
        (
            PSA_HOSTPATH + ', privileged (container "agent" must not set privileged=true)',
            403,
            "hostPath",
        ),
    ],
)
def test_t22_psa_allowance_rejects_wrong_violation_profile_status_or_rbac(
    message, status, expected
):
    result = a.admission_result(
        psa_error(message, status), a.POLICY, "sandbox-shape", psa_violation=expected
    )
    assert result["result"] == "error"


def test_t22_only_two_cases_allow_psa_and_kyverno_still_passes():
    cases = a.shape_cases(pod())
    assert {c.name for c in cases if c.psa_violation} == {"hostpath_volume", "hostnetwork_true"}
    for case in cases:
        result = a.admission_result(
            rejection(case.policy, case.rule),
            case.policy,
            case.rule,
            psa_violation=case.psa_violation,
        )
        assert result["result"] == "pass" and result["evidence"]["admission_layer"] == "kyverno"


def test_kubernetes_adapter_never_creates_forbidden_pods_or_ephemeral_containers():
    requests = []

    class Core:
        def create_namespaced_pod(self, namespace, body, **kwargs):
            requests.append((namespace, body, kwargs))
            raise rejection()

        def patch_namespaced_pod_ephemeralcontainers(self, name, namespace, body, **kwargs):
            requests.append((namespace, body, kwargs))
            raise rejection("agent-sandbox-deny-ephemeral-containers", "deny-ephemeral-containers")

    ops = a.KubernetesAdmission(Core(), None, "tenant-test")
    assert ops.admit(pod(), "agent-sandbox-admission", "sandbox-shape")["result"] == "pass"
    assert ops.attach(pod())["result"] == "pass"
    assert all(kw["dry_run"] == "All" for _, _, kw in requests)
    assert requests[1][1][0]["op"] == "add"
    assert requests[1][1][0]["path"] == "/spec/ephemeralContainers"
    assert requests[1][1][0]["value"][0]["targetContainerName"] == "agent"


def release(ready=True, signed=True):
    return {
        "metadata": {"name": "signed", "resourceVersion": "1"},
        "spec": {
            "agent": "conformance-b",
            "image": "registry/agent@sha256:2",
            "approval": {"approved": True, "approvedBy": "operator", "ticket": "fixture"},
        },
        "status": {
            "ready": ready,
            "signatureVerified": signed,
            "policyDrift": False,
            "podTemplateConfigMap": "podtemplate-signed",
            "policyDigest": "sha256:abc",
        },
    }


class Objects:
    """Models optimistic concurrency and controller-produced release state."""

    def __init__(self):
        self.objects = {
            ("agentreleases", "signed"): release(),
            ("agentreleases", "unsigned"): release(False, False),
            ("agents", "conformance-b"): {
                "metadata": {"name": "conformance-b", "resourceVersion": "1"},
                "spec": {"capabilities": {"mcp": [{"tools": ["pods_list_in_namespace"]}]}},
            },
        }
        self.patches = []
        self.objects[("agentreleases", "unsigned")]["metadata"]["name"] = "unsigned"
        self.objects[("agentreleases", "unsigned")]["spec"]["image"] = "registry/unsigned@sha256:3"

    def get_namespaced_custom_object(self, group, version, ns, plural, name, **kwargs):
        return copy.deepcopy(self.objects[(plural, name)])

    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body, **kwargs):
        obj = self.objects[(plural, name)]
        if body["metadata"]["resourceVersion"] != obj["metadata"]["resourceVersion"]:
            raise ApiException(status=409)
        assert set(body) == {"metadata", "spec"}  # no status write
        obj["spec"] = copy.deepcopy(body["spec"])
        obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)
        self.patches.append((plural, name, copy.deepcopy(body)))
        rel = self.objects[("agentreleases", "signed")]
        drift = self.objects[("agents", "conformance-b")]["spec"]["capabilities"]["mcp"][0][
            "tools"
        ] != ["pods_list_in_namespace"]
        rel["status"]["policyDrift"] = drift
        rel["status"]["ready"] = rel["spec"]["approval"]["approved"] and not drift
        return copy.deepcopy(obj)


@pytest.mark.parametrize("kind", ["unapproved", "drifted"])
async def test_mutation_restores_exact_spec_and_approval_even_when_probe_fails(kind):
    custom = Objects()
    before = copy.deepcopy(custom.objects)
    ops = a.KubernetesAdmission(None, custom, "tenant-test", timeout=0.1, poll=0)
    with pytest.raises(RuntimeError, match="probe failed"):
        async with a.changed_fixture(ops, "conformance-b", "signed", kind):
            assert not ops.release("signed")["status"]["ready"]
            raise RuntimeError("probe failed")
    for key, original in before.items():
        assert custom.objects[key]["spec"] == original["spec"]
    assert ops.release("signed")["status"]["ready"]
    assert len(custom.patches) == 2


async def test_restore_does_not_overwrite_concurrent_spec_edit():
    custom = Objects()
    ops = a.KubernetesAdmission(None, custom, "tenant-test", timeout=0.1, poll=0)
    with pytest.raises(a.FixtureConflict):
        async with a.changed_fixture(ops, "conformance-b", "signed", "unapproved"):
            custom.objects[("agentreleases", "signed")]["spec"]["approval"]["ticket"] = (
                "someone-else"
            )
    assert (
        custom.objects[("agentreleases", "signed")]["spec"]["approval"]["ticket"] == "someone-else"
    )
    assert len(custom.patches) == 1


async def test_patch_with_unknown_response_still_attempts_restore():
    class Uncertain(Objects):
        def patch_namespaced_custom_object(self, *args, **kw):
            result = super().patch_namespaced_custom_object(*args, **kw)
            if len(self.patches) == 1:
                raise TimeoutError("response lost after write")
            return result

    custom = Uncertain()
    ops = a.KubernetesAdmission(None, custom, "tenant-test", timeout=0.1, poll=0)
    with pytest.raises(TimeoutError, match="response lost"):
        async with a.changed_fixture(ops, "conformance-b", "signed", "unapproved"):
            pytest.fail("unknown mutation must not run a probe")
    assert ops.release("signed")["spec"]["approval"] == release()["spec"]["approval"]
    assert ops.release("signed")["status"]["ready"]


async def test_cancellation_restores_fixture_before_propagating():
    ops = a.KubernetesAdmission(None, Objects(), "tenant-test", timeout=0.1, poll=0)
    changed = asyncio.Event()

    async def probe():
        async with a.changed_fixture(ops, "conformance-b", "signed", "unapproved"):
            changed.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(probe())
    await changed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ops.release("signed")["spec"]["approval"] == release()["spec"]["approval"]
    assert ops.release("signed")["status"]["ready"]


def test_preflight_requires_generation_zero_exact_failure_and_no_pod_or_create_activity():
    evidence = {
        "outcome": "FAILED",
        "generation": 0,
        "activities": ["preflight", "stop_session"],
        "failures": [{"activity": "preflight", "type": "Forbidden", "non_retryable": True}],
        "pods": [],
    }
    assert a.evaluate_preflight(evidence)["result"] == "pass"
    for change in [
        {"generation": 1},
        {"activities": ["preflight", "create_sandbox"]},
        {"pods": ["unexpected"]},
        {"failures": [{"activity": "preflight", "type": "NotFound"}]},
        {"outcome": "COMPLETED"},
    ]:
        assert a.evaluate_preflight(evidence | change)["result"] == "fail"


class ScenarioOps(a.KubernetesAdmission):
    def __init__(self):
        self.custom_fixture = Objects()
        super().__init__(None, self.custom_fixture, "tenant-test", timeout=0.1, poll=0)
        self.control_ok = True
        self.ran = []

    def template(self, rel):
        return pod()

    def admit(self, candidate, policy=None, rule=None, *, psa_violation=None):
        self.ran.append((policy, rule))
        if policy is None and not self.control_ok:
            return a.admission_result(rejection())
        return a.admission_result(rejection(policy, rule) if policy else None, policy, rule)

    def attach(self, candidate):
        return a.admission_result(
            rejection("agent-sandbox-deny-ephemeral-containers", "deny-ephemeral-containers"),
            "agent-sandbox-deny-ephemeral-containers",
            "deny-ephemeral-containers",
        )


class Sessions:
    async def refusal(self, agent, rel):
        return {
            "outcome": "FAILED",
            "generation": 0,
            "activities": ["preflight"],
            "failures": [{"activity": "preflight", "type": "Forbidden", "non_retryable": True}],
            "pods": [],
        }

    @asynccontextmanager
    async def live_pod(self, agent, rel):
        yield pod()


def harness(ops=None, **kw):
    return a.AdmissionHarness(ops or ScenarioOps(), Sessions(), "conformance-b", "signed", **kw)


async def test_t22_requires_positive_control_before_negative_cases():
    ops = ScenarioOps()
    ops.control_ok = False
    result = await harness(ops).t22()
    assert result["result"] == "error" and len(ops.ran) == 1


async def test_t22_records_all_nine_shapes_and_live_pod_attach():
    result = await harness().t22()
    assert result["result"] == "pass"
    assert len(result["cases"]) == 11  # positive control, 9 mutations, attach
    assert result["cases"][-1]["case"] == "ephemeral_attach"
    assert result["evidence"]["pod_requests"] == "server-side dry-run"


@pytest.mark.parametrize("test", ["t9a", "t9b"])
async def test_mutable_tests_require_explicit_opt_in_and_unsigned_fixture(test):
    assert (await getattr(harness(), test)())["result"] == "error"
    assert (await getattr(harness(allow_mutations=True), test)())["result"] == "error"


@pytest.mark.parametrize("test", ["t9a", "t9b"])
async def test_mutable_tests_complete_and_restore_without_status_writes(test):
    ops = ScenarioOps()
    original = copy.deepcopy(ops.custom_fixture.objects)
    result = await getattr(harness(ops, allow_mutations=True, unsigned_release="unsigned"), test)()
    assert result["result"] == "pass"
    for key, obj in original.items():
        assert ops.custom_fixture.objects[key]["spec"] == obj["spec"]
    assert result["checks"]["fixture_restored"]
    if test == "t9a":
        assert any(c["case"] == "runner_positive_control" for c in result["cases"])


async def test_signed_or_unapproved_unsigned_fixture_cannot_prove_signature_refusal():
    for field in ("signed", "unapproved"):
        ops = ScenarioOps()
        unsigned = ops.custom_fixture.objects[("agentreleases", "unsigned")]
        if field == "signed":
            unsigned["status"]["signatureVerified"] = True
        else:
            unsigned["spec"]["approval"]["approved"] = False
        result = await harness(ops, allow_mutations=True, unsigned_release="unsigned").t9a()
        assert result["result"] == "error"
        assert not ops.custom_fixture.patches


def refusal_events():
    schedule = HistoryEvent(event_id=2, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
    schedule.activity_task_scheduled_event_attributes.activity_type.name = "preflight"
    failure = HistoryEvent(event_id=3, event_type=EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED)
    attrs = failure.activity_task_failed_event_attributes
    attrs.scheduled_event_id = 2
    attrs.failure.application_failure_info.type = "Forbidden"
    attrs.failure.application_failure_info.non_retryable = True
    return [schedule, failure]


class Handle:
    def __init__(self, running=False):
        self.running = running
        self.finished = asyncio.Event()
        self.cancelled = False

    async def result(self):
        if self.running:
            await self.finished.wait()
        return SessionResult("FAILED", generation=0)

    async def fetch_history_events(self):
        for event in refusal_events():
            yield event

    async def cancel(self):
        self.cancelled = True
        self.finished.set()


class SessionOps:
    tenant = "tenant-test"
    poll = 0
    timeout = 0.1

    def __init__(self, handle, valid=True):
        self.handle, self.valid = handle, valid

    def pods(self, session):
        if not self.handle.running or self.handle.cancelled:
            return []
        candidate = pod()
        candidate["metadata"]["labels"]["agentplatform.io/session"] = session
        candidate["status"] = {"phase": "Running" if self.valid else "Pending"}
        return [candidate]


async def test_temporal_driver_records_true_generation_zero_and_preflight_history():
    handle = Handle()

    async def start(client, inp):
        assert inp.agent == "conformance-b" and inp.release == "unsigned"
        return handle

    driver = a.AdmissionSessions(None, SessionOps(handle), submit=start)
    evidence = await driver.refusal("conformance-b", "unsigned")
    assert evidence["generation"] == 0
    assert evidence["activities"] == ["preflight"] and evidence["pods"] == []
    assert a.evaluate_preflight(evidence)["result"] == "pass"


@pytest.mark.parametrize("valid", [True, False])
async def test_live_sandbox_is_cancelled_and_absent_even_when_attach_or_readiness_fails(valid):
    handle = Handle(running=True)

    async def start(client, inp):
        return handle

    driver = a.AdmissionSessions(None, SessionOps(handle, valid), submit=start, timeout=0.02)
    with pytest.raises((RuntimeError, TimeoutError)):
        async with driver.live_pod("conformance-b", "signed") as candidate:
            assert candidate["status"]["phase"] == "Running"
            raise RuntimeError("attach failure")
    assert handle.cancelled


def test_cli_requires_explicit_separate_mutation_fixture():
    from runner.conformance import parse_args

    for extra in [
        [],
        ["--admission-fixture-agent", "conformance", "--admission-fixture-release", "r"],
    ]:
        with pytest.raises(SystemExit):
            parse_args(["--release", "r", "--tests", "T9a", "--allow-admission-mutations", *extra])
    args = parse_args(
        [
            "--release",
            "r",
            "--tests",
            "T9a,T9b,T22",
            "--allow-admission-mutations",
            "--admission-fixture-agent",
            "conformance-b",
            "--admission-fixture-release",
            "signed",
            "--admission-unsigned-release",
            "unsigned",
        ]
    )
    assert args.admission_fixture_agent == "conformance-b"
    assert parse_args(["--release", "r", "--tests", "T22"]).tests == ["T22"]
