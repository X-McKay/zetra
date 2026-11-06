"""T9a/T9b/T22: admission evidence and explicitly authorized, reversible test fixtures.

Malformed Pods and ephemeral containers are always server-side dry-runs. Only the
runner creates the valid sandbox used for the attach test. No status is written.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass

import yaml
from kubernetes.client.exceptions import ApiException
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError

from runner.submit import submit as submit_session
from runner.types import SessionInput

GROUP, VERSION = "agentplatform.io", "v1alpha1"
LABEL = "agentplatform.io/"
POLICY = "agent-sandbox-admission"
RELEASE_RULE = "release-must-be-ready-and-match"
PSA_VIOLATIONS = {
    "hostPath": 'restricted volume types (volume "forbidden" uses restricted volume type "hostPath")',
    "hostNetwork": "host namespaces (hostNetwork=true)",
}


class FixtureConflict(RuntimeError):
    """A concurrent spec change must not be overwritten by fixture restoration."""


def admission_result(
    error=None,
    policy: str | None = None,
    rule: str | None = None,
    *,
    psa_violation: str | None = None,
) -> dict:
    text = str(getattr(error, "body", "") or getattr(error, "reason", ""))[:8192]
    try:
        status_body = json.loads(text)
        if isinstance(status_body, dict):
            text = str(status_body.get("message", text))
    except ValueError:
        pass
    status = getattr(error, "status", None)
    accepted = error is None
    block = re.search(rf"(?m)^{re.escape(policy or '')}:\s*\n((?:[ \t].*(?:\n|$))*)", text)
    named = bool(
        error is not None
        and status in (400, 403, 422)
        and re.search(r'admission webhook ["\'].*kyverno', text)
        and policy
        and rule
        and block
        and re.search(rf"(?m)^[ \t]+{re.escape(rule)}:", block.group(1))
    )
    # T22 requires rejection, not a particular admission layer. PSA runs before
    # Kyverno and these two malformed shapes intrinsically violate Restricted.
    # Opt in per shape; all T9b cases keep the named-Kyverno requirement.
    psa_match = re.fullmatch(
        r'pods "[a-z0-9.-]+" is forbidden: violates PodSecurity "(restricted:v1\.[0-9]+)": (.+)',
        text,
    )
    psa = bool(
        policy
        and status == 403
        and psa_violation in PSA_VIOLATIONS
        and psa_match
        and psa_match.group(2) == PSA_VIOLATIONS[psa_violation]
    )
    if policy is None:
        result = "pass" if accepted else "error"
        checks = {"positive_control_admitted": accepted}
    else:
        result = "pass" if named or psa else ("fail" if accepted else "error")
        checks = {
            "rejected": not accepted,
            **({"expected_admission": named or psa} if psa_violation else {"expected_rule": named}),
        }
    return {
        "result": result,
        "checks": checks,
        "evidence": {
            "accepted": accepted,
            "status": status,
            "message": text,
            "expected_policy": policy,
            "expected_rule": rule,
            "expected_psa_violation": psa_violation,
            "admission_layer": "kyverno" if named else "pod-security-admission" if psa else None,
            "pod_security_policy": psa_match.group(1) if psa else None,
            "pod_security_violation": psa_match.group(2) if psa else None,
        },
    }


def fresh_pod(template: dict) -> dict:
    pod = copy.deepcopy(template)
    sid = str(uuid.uuid4())
    pod["metadata"]["name"] = "admission-" + sid
    pod["metadata"].pop("generateName", None)
    for key in ("uid", "resourceVersion", "creationTimestamp", "managedFields"):
        pod["metadata"].pop(key, None)
    pod.pop("status", None)
    pod["metadata"]["labels"].update({LABEL + "session": sid, LABEL + "generation": "1"})
    return pod


@dataclass
class PodCase:
    name: str
    pod: dict
    rule: str
    policy: str = POLICY
    psa_violation: str | None = None


def shape_cases(template: dict) -> list[PodCase]:
    cases = []
    for name, rule in (
        ("extra_container", "sandbox-no-extras"),
        ("changed_init_image", "sandbox-shape"),
        ("hostpath_volume", "sandbox-no-extras"),
        ("wrong_serviceaccount", "sandbox-shape"),
        ("no_sandbox_label", "tenant-pods-are-sandboxes"),
        ("automount_true", "sandbox-shape"),
        ("automount_omitted", "sandbox-shape"),
        ("runtimeclass_omitted", "sandbox-shape"),
        ("hostnetwork_true", "sandbox-shape"),
    ):
        p = fresh_pod(template)
        spec = p["spec"]
        if name == "extra_container":
            extra = copy.deepcopy(spec["containers"][0])
            extra["name"] = "extra"
            spec["containers"].append(extra)
        elif name == "changed_init_image":
            spec["initContainers"][0]["image"] = spec["containers"][0]["image"]
        elif name == "hostpath_volume":
            spec["volumes"].append({"name": "forbidden", "hostPath": {"path": "/"}})
        elif name == "wrong_serviceaccount":
            spec["serviceAccountName"] = "default"
        elif name == "no_sandbox_label":
            del p["metadata"]["labels"][LABEL + "sandbox"]
        elif name == "automount_true":
            spec["automountServiceAccountToken"] = True
        elif name == "automount_omitted":
            spec.pop("automountServiceAccountToken", None)
        elif name == "runtimeclass_omitted":
            spec.pop("runtimeClassName", None)
        elif name == "hostnetwork_true":
            spec["hostNetwork"] = True
        cases.append(
            PodCase(
                name,
                p,
                rule,
                psa_violation={
                    "hostpath_volume": "hostPath",
                    "hostnetwork_true": "hostNetwork",
                }.get(name),
            )
        )
    return cases


class KubernetesAdmission:
    def __init__(self, core, custom, tenant: str, *, timeout: float = 120, poll: float = 1):
        self.core, self.custom, self.tenant = core, custom, tenant
        self.timeout, self.poll = timeout, poll

    def object(self, plural: str, name: str) -> dict:
        return self.custom.get_namespaced_custom_object(
            GROUP, VERSION, self.tenant, plural, name, _request_timeout=15
        )

    def release(self, name: str) -> dict:
        return self.object("agentreleases", name)

    def patch_spec(self, plural: str, name: str, current: dict, spec: dict) -> dict:
        # Kubernetes checks this resourceVersion atomically. A 409 is an error, not a retry
        # that silently rebases our test mutation over somebody else's edit.
        return self.custom.patch_namespaced_custom_object(
            GROUP,
            VERSION,
            self.tenant,
            plural,
            name,
            {"metadata": {"resourceVersion": current["metadata"]["resourceVersion"]}, "spec": spec},
            _request_timeout=15,
        )

    def wait_release(self, name: str, predicate) -> dict:
        until = time.monotonic() + self.timeout
        while True:
            obj = self.release(name)
            if predicate(obj):
                return obj
            if time.monotonic() >= until:
                raise TimeoutError("controller did not reconcile admission fixture")
            time.sleep(self.poll)

    def template(self, rel: dict) -> dict:
        name = rel["status"]["podTemplateConfigMap"]
        cm = self.core.read_namespaced_config_map(name, self.tenant, _request_timeout=15)
        template = yaml.safe_load(cm.data["pod.yaml"])
        if (
            not isinstance(template, dict)
            or template.get("kind") != "Pod"
            or template["metadata"]["labels"][LABEL + "agent"] != rel["spec"]["agent"]
            or template["metadata"]["labels"][LABEL + "release"] != rel["metadata"]["name"]
            or template["spec"]["containers"][0]["image"] != rel["spec"]["image"]
            or template["metadata"]["annotations"][LABEL + "policy-digest"]
            != rel["status"]["policyDigest"]
        ):
            raise ValueError("controller pod template does not match fixture release")
        return template

    def admit(self, pod: dict, policy=None, rule=None, *, psa_violation=None) -> dict:
        try:
            self.core.create_namespaced_pod(self.tenant, pod, dry_run="All", _request_timeout=30)
        except ApiException as exc:
            return admission_result(exc, policy, rule, psa_violation=psa_violation)
        return admission_result(None, policy, rule, psa_violation=psa_violation)

    def attach(self, pod: dict) -> dict:
        # The Kubernetes client's first advertised PATCH media type is JSON Patch.
        body = [
            {
                "op": "add",
                "path": "/spec/ephemeralContainers",
                "value": [
                    {
                        "name": "conformance-debug",
                        "targetContainerName": "agent",
                        "image": pod["spec"]["containers"][0]["image"],
                        "command": ["true"],
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 1500,
                            "allowPrivilegeEscalation": False,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }
                ],
            }
        ]
        policy, rule = "agent-sandbox-deny-ephemeral-containers", "deny-ephemeral-containers"
        try:
            self.core.patch_namespaced_pod_ephemeralcontainers(
                pod["metadata"]["name"], self.tenant, body, dry_run="All", _request_timeout=30
            )
        except ApiException as exc:
            return admission_result(exc, policy, rule)
        return admission_result(None, policy, rule)

    def pods(self, session: str) -> list[dict]:
        found = self.core.list_namespaced_pod(
            self.tenant, label_selector=f"{LABEL}session={session}", _request_timeout=15
        )
        return [self.core.api_client.sanitize_for_serialization(p) for p in found.items]


def ready(rel: dict) -> bool:
    status = rel.get("status") or {}
    return (
        rel.get("spec", {}).get("approval", {}).get("approved") is True
        and status.get("ready") is True
        and status.get("signatureVerified") is True
        and status.get("policyDrift") is False
    )


@asynccontextmanager
async def changed_fixture(ops: KubernetesAdmission, agent: str, release: str, kind: str):
    plural, name = ("agentreleases", release) if kind == "unapproved" else ("agents", agent)
    original = await asyncio.to_thread(ops.object, plural, name)
    spec = copy.deepcopy(original["spec"])
    if kind == "unapproved":
        spec["approval"]["approved"] = False
    elif kind == "drifted":
        spec["capabilities"]["mcp"][0]["tools"].append("conformance_drift_" + uuid.uuid4().hex)
    else:
        raise ValueError("unknown fixture mutation")
    attempted = False
    try:
        attempted = True
        await settled(asyncio.to_thread(ops.patch_spec, plural, name, original, spec))

        def reconciled(rel):
            status = rel.get("status") or {}
            return status.get("ready") is False and (
                rel["spec"]["approval"]["approved"] is False
                if kind == "unapproved"
                else status.get("policyDrift") is True
            )

        await asyncio.to_thread(ops.wait_release, release, reconciled)
        yield
    finally:

        async def restore():
            current = await asyncio.to_thread(ops.object, plural, name)
            if current["spec"] != original["spec"]:
                if current["spec"] != spec:
                    raise FixtureConflict("fixture spec changed concurrently; restoration refused")
                await asyncio.to_thread(ops.patch_spec, plural, name, current, original["spec"])
            await asyncio.to_thread(ops.wait_release, release, ready)

        if attempted:
            # Even a lost PATCH response can mean the mutation reached the API server.
            await settled(restore())


async def settled(awaitable):
    """Finish a mutation or cleanup before propagating harness cancellation."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


class AdmissionSessions:
    """Real Temporal sessions; evidence comes from history and the Kubernetes pod list."""

    def __init__(self, client, ops, *, submit=submit_session, timeout: float = 240):
        self.client, self.ops, self.submit, self.timeout = client, ops, submit, timeout

    async def _start(self, agent: str, release: str):
        inp = SessionInput(
            self.ops.tenant,
            agent,
            release,
            str(uuid.uuid4()),
            None,
            {"tool": "probe_files", "args": {}},
        )
        handle = await self.submit(self.client, inp)
        return inp, handle, asyncio.create_task(handle.result())

    async def _close(self, inp, handle, result_task):
        if not result_task.done():
            await handle.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(result_task), self.timeout)
        except WorkflowFailureError:
            pass  # cancellation is expected; pod absence below is still required
        until = time.monotonic() + self.timeout
        while await asyncio.to_thread(self.ops.pods, inp.session_id):
            if time.monotonic() >= until:
                raise TimeoutError("admission control sandbox deletion not confirmed")
            await asyncio.sleep(self.ops.poll)

    async def refusal(self, agent: str, release: str) -> dict:
        inp, handle, result_task = await self._start(agent, release)
        try:
            result = await asyncio.wait_for(asyncio.shield(result_task), self.timeout)
            events = [event async for event in handle.fetch_history_events()]
            scheduled = {
                e.event_id: e.activity_task_scheduled_event_attributes.activity_type.name
                for e in events
                if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
            }
            failures = []
            for e in events:
                if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED:
                    attrs = e.activity_task_failed_event_attributes
                    info = attrs.failure.application_failure_info
                    failures.append(
                        {
                            "activity": scheduled.get(attrs.scheduled_event_id),
                            "type": info.type,
                            "non_retryable": info.non_retryable,
                        }
                    )
            pods = await asyncio.to_thread(self.ops.pods, inp.session_id)
            return {
                "session_id": inp.session_id,
                "outcome": result.outcome,
                "generation": result.generation,
                "activities": list(scheduled.values()),
                "failures": failures,
                "pods": [p["metadata"]["name"] for p in pods],
            }
        finally:
            await settled(self._close(inp, handle, result_task))

    @asynccontextmanager
    async def live_pod(self, agent: str, release: str):
        inp, handle, result_task = await self._start(agent, release)
        try:
            until = time.monotonic() + self.timeout
            while True:
                pods = await asyncio.to_thread(self.ops.pods, inp.session_id)
                for p in pods:
                    labels = p["metadata"].get("labels") or {}
                    if (
                        p.get("status", {}).get("phase") == "Running"
                        and not p["metadata"].get("deletionTimestamp")
                        and labels.get(LABEL + "sandbox") == "true"
                        and labels.get(LABEL + "agent") == agent
                        and labels.get(LABEL + "release") == release
                    ):
                        yield p
                        return
                if result_task.done() or time.monotonic() >= until:
                    raise TimeoutError("no live valid runner sandbox for ephemeral attach")
                await asyncio.sleep(self.ops.poll)
        finally:
            await settled(self._close(inp, handle, result_task))


def evaluate_preflight(evidence: dict) -> dict:
    checks = {
        "session_failed": evidence.get("outcome") == "FAILED",
        "generation_zero": evidence.get("generation") == 0,
        "preflight_forbidden": any(
            f.get("activity") == "preflight"
            and f.get("type") == "Forbidden"
            and f.get("non_retryable") is True
            for f in evidence.get("failures", [])
        ),
        "preflight_scheduled": "preflight" in evidence.get("activities", []),
        "no_create_sandbox": "create_sandbox" not in evidence.get("activities", []),
        "no_generation_set": "set_generation" not in evidence.get("activities", []),
        "no_pod": evidence.get("pods") == [],
    }
    return {
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "evidence": evidence,
    }


class AdmissionHarness:
    def __init__(
        self,
        ops,
        sessions,
        agent: str,
        release: str,
        *,
        unsigned_release: str | None = None,
        allow_mutations: bool = False,
    ):
        self.ops, self.sessions, self.agent, self.release = ops, sessions, agent, release
        self.unsigned_release, self.allow_mutations = unsigned_release, allow_mutations

    async def _base(self) -> tuple[dict, dict]:
        rel = await asyncio.to_thread(self.ops.release, self.release)
        if rel["spec"]["agent"] != self.agent or not ready(rel):
            raise ValueError("fixture must be an approved, signed, drift-free Ready release")
        template = await asyncio.to_thread(self.ops.template, rel)
        control = {
            "case": "positive_control",
            **await asyncio.to_thread(self.ops.admit, fresh_pod(template)),
        }
        return template, control

    async def _unsigned(self) -> dict:
        if not self.allow_mutations or not self.unsigned_release:
            raise ValueError(
                "T9a/T9b require explicit fixture mutation consent and unsigned release"
            )
        rel = await asyncio.to_thread(self.ops.release, self.unsigned_release)
        status = rel.get("status") or {}
        if (
            rel["spec"]["agent"] != self.agent
            or rel["spec"]["approval"]["approved"] is not True
            or status.get("ready") is not False
            or status.get("signatureVerified") is not False
            or status.get("policyDrift") is not False
        ):
            raise ValueError("unsigned fixture must be approved and controller-confirmed unsigned")
        return rel

    async def _case(self, case: PodCase) -> dict:
        return {
            "case": case.name,
            **await asyncio.to_thread(
                self.ops.admit, case.pod, case.policy, case.rule, psa_violation=case.psa_violation
            ),
        }

    def _report(self, test: str, cases: list[dict], **extra) -> dict:
        results = [c["result"] for c in cases]
        return {
            "id": test,
            "name": {
                "T9a": "preflight refusals",
                "T9b": "release admission",
                "T22": "admission completeness",
            }[test],
            "result": "error" if "error" in results else "fail" if "fail" in results else "pass",
            "cases": cases,
            "evidence": {
                "fixture_agent": self.agent,
                "fixture_release": self.release,
                "pod_requests": "server-side dry-run",
            },
            **extra,
        }

    async def _run(self, test: str) -> dict:
        cases = []
        restored = False
        try:
            # Reject missing opt-in before even performing the positive-control API request.
            unsigned = await self._unsigned() if test != "T22" else None
            template, control = await self._base()
            cases.append(control)
            if control["result"] != "pass":
                return self._report(test, cases)
            if test == "T22":
                for case in shape_cases(template):
                    cases.append(await self._case(case))
                async with self.sessions.live_pod(self.agent, self.release) as live:
                    cases.append(
                        {
                            "case": "ephemeral_attach",
                            **await asyncio.to_thread(self.ops.attach, live),
                        }
                    )
            elif test == "T9a":
                # A real signed fixture must pass preflight too; otherwise an always-refuse
                # runner could make every negative case look correct.
                async with self.sessions.live_pod(self.agent, self.release) as live:
                    cases.append(
                        {
                            "case": "runner_positive_control",
                            "result": "pass",
                            "checks": {"live_runner_sandbox": True},
                            "evidence": {"pod": live["metadata"]["name"]},
                        }
                    )
                cases.append(
                    {
                        "case": "unsigned",
                        **evaluate_preflight(
                            await self.sessions.refusal(self.agent, self.unsigned_release)
                        ),
                    }
                )
                async with changed_fixture(self.ops, self.agent, self.release, "unapproved"):
                    cases.append(
                        {
                            "case": "unapproved",
                            **evaluate_preflight(
                                await self.sessions.refusal(self.agent, self.release)
                            ),
                        }
                    )
                restored = True
            else:
                p = fresh_pod(template)
                p["spec"]["containers"][0]["image"] = unsigned["spec"]["image"]
                p["metadata"]["labels"][LABEL + "release"] = self.unsigned_release
                if unsigned.get("status", {}).get("policyDigest"):
                    p["metadata"]["annotations"][LABEL + "policy-digest"] = unsigned["status"][
                        "policyDigest"
                    ]
                cases.append(
                    await self._case(
                        PodCase("unsigned", p, "verify-image-signature", "agent-image-signature")
                    )
                )
                for kind in ("unapproved", "drifted"):
                    async with changed_fixture(self.ops, self.agent, self.release, kind):
                        cases.append(
                            await self._case(PodCase(kind, fresh_pod(template), RELEASE_RULE))
                        )
                restored = True
                p = fresh_pod(template)
                p["metadata"]["annotations"][LABEL + "policy-digest"] = "sha256:" + "0" * 64
                cases.append(await self._case(PodCase("policy_digest_mismatch", p, RELEASE_RULE)))
                p = fresh_pod(template)
                p["metadata"]["labels"][LABEL + "agent"] = "admission-other"
                cases.append(await self._case(PodCase("agent_label_mismatch", p, RELEASE_RULE)))
            return self._report(
                test, cases, checks={"fixture_restored": restored} if test != "T22" else {}
            )
        except Exception as exc:  # preserve earlier cases when setup, cleanup or API access fails
            result = self._report(test, cases, checks={"fixture_restored": restored})
            result.update(result="error", error=type(exc).__name__, detail=str(exc)[:1024])
            return result

    async def t9a(self) -> dict:
        return await self._run("T9a")

    async def t9b(self) -> dict:
        return await self._run("T9b")

    async def t22(self) -> dict:
        return await self._run("T22")
