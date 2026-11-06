"""T15/T20: active-session revocation with exact, explicitly authorized test fixtures."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from kubernetes.client.exceptions import ApiException

from runner.conformance_admission import (
    AdmissionSessions,
    FixtureConflict,
    KubernetesAdmission,
    evaluate_preflight,
    ready,
    settled,
)
from runner.submit import submit
from runner.types import SessionInput

HOLD_POLICY = "conformance-revocation-delete-hold"
HOLD_RULE = "hold-conformance-session"
INERT_NAME = "conformance-revocation-inert"
INERT_UID = "00000000-0000-0000-0000-000000000000"
FIXTURE_AGENT = "conformance-b"
LABEL = "agentplatform.io/"
RECONCILE = "kustomize.toolkit.fluxcd.io/reconcile"


def merge_patch(old, new):
    """A merge patch that restores omitted fields as well as existing values."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return copy.deepcopy(new)
    result = {k: None for k in old.keys() - new.keys()}
    for key, value in new.items():
        if key not in old or old[key] != value:
            result[key] = merge_patch(old.get(key), value)
    return result


def immutable_image_result(error) -> dict:
    message = ""
    if error is not None:
        try:
            message = json.loads(error.body).get("message", "")
        except (TypeError, ValueError):
            pass
    checks = {
        "rejected_422": getattr(error, "status", None) == 422,
        "image_immutable_rule": "image is immutable" in message,
    }
    return {
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "evidence": {
            "status": getattr(error, "status", None),
            "message": message[:1024],
            "dry_run": True,
        },
    }


def deletion_event(events: list[dict], name: str, uid: str, policy_uid: str) -> dict | None:
    for event in events:
        obj = event.get("involvedObject") or {}
        related = event.get("related") or {}
        message = event.get("message", "")
        if (
            obj.get("kind") == "Policy"
            and obj.get("name") == HOLD_POLICY
            and bool(policy_uid)
            and obj.get("uid") == policy_uid
            and related.get("kind") == "Pod"
            and related.get("name") == name
            and related.get("uid") == uid
            and event.get("reason") == "PolicyViolation"
            and event.get("action") == "Resource Blocked"
            and f"[{HOLD_RULE}] fail (blocked)" in message
        ):
            return {
                k: event.get(k)
                for k in (
                    "reason",
                    "action",
                    "message",
                    "involvedObject",
                    "related",
                    "lastTimestamp",
                    "eventTime",
                )
            }
    return None


class RevocationOps(KubernetesAdmission):
    """Only fixed Agent/Policy names can be mutated; no Pod writes or status writes."""

    def policy(self):
        return self.custom.get_namespaced_custom_object(
            "kyverno.io",
            "v1",
            self.tenant,
            "policies",
            HOLD_POLICY,
            _request_timeout=5,
        )

    def patch_rule(self, current, rule):
        return self.custom.patch_namespaced_custom_object(
            "kyverno.io",
            "v1",
            self.tenant,
            "policies",
            HOLD_POLICY,
            {
                "metadata": {"resourceVersion": current["metadata"]["resourceVersion"]},
                "spec": {"rules": [rule]},
            },
            _request_timeout=5,
        )

    def patch_agent(self, current, spec=None, annotation=...):
        if current["metadata"]["name"] != FIXTURE_AGENT:
            raise ValueError("revocation mutations are restricted to conformance-b")
        body = {"metadata": {"resourceVersion": current["metadata"]["resourceVersion"]}}
        if spec is not None:
            body["spec"] = merge_patch(current["spec"], spec)
        if annotation is not ...:
            body["metadata"]["annotations"] = {RECONCILE: annotation}
        return self.custom.patch_namespaced_custom_object(
            "agentplatform.io",
            "v1alpha1",
            self.tenant,
            "agents",
            FIXTURE_AGENT,
            body,
            _request_timeout=5,
        )

    def immutable_image(self, release):
        image = release["spec"]["image"]
        replacement = (
            image.rsplit("@sha256:", 1)[0]
            + "@sha256:"
            + ("0" if not image.endswith("0" * 64) else "1") * 64
        )
        try:
            self.custom.patch_namespaced_custom_object(
                "agentplatform.io",
                "v1alpha1",
                self.tenant,
                "agentreleases",
                release["metadata"]["name"],
                {
                    "metadata": {"resourceVersion": release["metadata"]["resourceVersion"]},
                    "spec": {"image": replacement},
                },
                dry_run="All",
                _request_timeout=5,
            )
        except ApiException as error:
            return immutable_image_result(error)
        return immutable_image_result(None)

    def agent_pods(self):
        result = self.core.list_namespaced_pod(
            self.tenant,
            label_selector=LABEL + "agent=" + FIXTURE_AGENT,
            _request_timeout=5,
        )
        return [self.core.api_client.sanitize_for_serialization(p) for p in result.items]

    def events(self):
        result = self.core.list_namespaced_event(
            self.tenant,
            field_selector="involvedObject.name=" + HOLD_POLICY,
            _request_timeout=5,
        )
        return [self.core.api_client.sanitize_for_serialization(e) for e in result.items]

    def invariant(self, release):
        obj = self.release(release)
        status = obj["status"]
        names = [status["policyBundleConfigMap"], status["podTemplateConfigMap"]]
        return {
            "policyDigest": status["policyDigest"],
            "configmaps": {
                name: hashlib.sha256(
                    json.dumps(
                        self.core.read_namespaced_config_map(
                            name, self.tenant, _request_timeout=5
                        ).data,
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                for name in names
            },
        }


class AgentFixture:
    def __init__(self, ops, release):
        self.ops, self.release = ops, release
        self.original = self.expected = None
        self.restored = False

    async def __aenter__(self):
        if await asyncio.to_thread(self.ops.agent_pods):
            raise FixtureConflict("conformance-b already has a sandbox")
        self.original = await asyncio.to_thread(self.ops.object, "agents", FIXTURE_AGENT)
        if not self.original["metadata"].get("uid"):
            raise FixtureConflict("Agent fixture has no immutable UID")
        self.expected = copy.deepcopy(self.original["spec"])
        if self.expected.get("enabled", True) is not True:
            raise FixtureConflict("conformance-b must initially be enabled")
        # Set before attempting PATCH, so a lost response still restores the annotation.
        try:
            await settled(
                asyncio.to_thread(self.ops.patch_agent, self.original, annotation="disabled")
            )
        except BaseException:
            await settled(self.finish())
            raise
        return self

    async def current(self):
        current = await asyncio.to_thread(self.ops.object, "agents", FIXTURE_AGENT)
        if current["metadata"].get("uid") != self.original["metadata"]["uid"]:
            raise FixtureConflict("Agent fixture was replaced; mutation/restoration refused")
        return current

    async def change(self, spec):
        current = await self.current()
        if current["spec"] != self.expected:
            raise FixtureConflict("Agent changed concurrently")
        self.expected = copy.deepcopy(spec)
        timing = {"request_start_ns": time.monotonic_ns(), "request_epoch_ns": time.time_ns()}
        await settled(asyncio.to_thread(self.ops.patch_agent, current, spec))
        timing["acknowledged_ns"] = time.monotonic_ns()
        return timing

    async def restore(self):
        current = await self.current()
        if current["spec"] != self.original["spec"]:
            if current["spec"] != self.expected:
                raise FixtureConflict("Agent changed concurrently; restoration refused")
            await asyncio.to_thread(self.ops.patch_agent, current, self.original["spec"])
        self.expected = copy.deepcopy(self.original["spec"])
        await asyncio.to_thread(self.ops.wait_release, self.release, ready)

    async def finish(self):
        await self.restore()
        current = await self.current()
        annotation = current["metadata"].get("annotations", {}).get(RECONCILE)
        original = self.original["metadata"].get("annotations", {}).get(RECONCILE)
        if annotation != original:
            if annotation != "disabled":
                raise FixtureConflict("Agent reconciliation annotation changed concurrently")
            await asyncio.to_thread(self.ops.patch_agent, current, annotation=original)
        final = await self.current()
        self.restored = (
            final["spec"] == self.original["spec"]
            and final["metadata"].get("annotations", {}).get(RECONCILE) == original
        )
        if not self.restored:
            raise FixtureConflict("Agent restoration not confirmed")

    async def __aexit__(self, *_):
        await settled(self.finish())


def hold_rule(name: str = INERT_NAME, uid: str = INERT_UID) -> dict:
    if (name, uid) != (INERT_NAME, INERT_UID):
        if not re.fullmatch(r"s-[0-9a-f]{16}-g[1-3]", name):
            raise ValueError("deletion hold needs one exact sandbox name")
        if str(uuid.UUID(uid)) != uid or uid == INERT_UID:
            raise ValueError("deletion hold needs the sandbox UID")
    return {
        "name": HOLD_RULE,
        "skipBackgroundRequests": True,
        "match": {
            "any": [{"resources": {"kinds": ["Pod"], "operations": ["DELETE"], "names": [name]}}]
        },
        "validate": {
            "allowExistingViolations": False,
            "failureAction": "Enforce",
            "message": "conformance cancellation measurement holds this exact sandbox",
            "deny": {
                "conditions": {
                    "all": [
                        {
                            "key": "{{ request.oldObject.metadata.uid }}",
                            "operator": "Equals",
                            "value": uid,
                        },
                        {
                            "key": "{{ request.oldObject.metadata.labels.\"agentplatform.io/agent\" || '' }}",
                            "operator": "Equals",
                            "value": FIXTURE_AGENT,
                        },
                    ]
                }
            },
        },
    }


@asynccontextmanager
async def deletion_hold(ops, name: str, uid: str):
    original = await asyncio.to_thread(ops.policy)
    if not original["metadata"].get("uid"):
        raise FixtureConflict("deletion Policy fixture has no immutable UID")
    inert = hold_rule()
    if original.get("spec", {}).get("background") is not False or original["spec"].get("rules") != [
        inert
    ]:
        raise FixtureConflict("deletion policy is not the expected inert fixture")
    active = hold_rule(name, uid)
    receipt = {"restored": False, "policy_uid": original["metadata"]["uid"]}
    attempted = False
    try:
        attempted = True
        await settled(asyncio.to_thread(ops.patch_rule, original, active))
        yield receipt
    finally:

        async def restore():
            current = await asyncio.to_thread(ops.policy)
            if current["metadata"].get("uid") != original["metadata"].get("uid"):
                raise FixtureConflict("deletion Policy was replaced; restoration refused")
            rules = current["spec"].get("rules")
            if {k: v for k, v in current["spec"].items() if k != "rules"} != {
                k: v for k, v in original["spec"].items() if k != "rules"
            }:
                raise FixtureConflict("deletion policy changed concurrently; restoration refused")
            if rules != [inert]:
                if rules != [active]:
                    raise FixtureConflict("deletion hold changed concurrently; restoration refused")
                await asyncio.to_thread(ops.patch_rule, current, inert)
            final = await asyncio.to_thread(ops.policy)
            if (
                final["metadata"].get("uid") != receipt["policy_uid"]
                or final["spec"] != original["spec"]
            ):
                raise FixtureConflict("deletion hold restoration not confirmed")
            receipt["restored"] = True

        if attempted:
            await settled(restore())


def denial_evidence(
    items, replicas: set[str], started_ns: int, reason: str, limit: float | None
) -> dict:
    first = {}
    for item in sorted(items, key=lambda i: i.get("observed_ns", 0)):
        source = item.get("source_pod")
        if (
            source in replicas
            and item.get("reason") == reason
            and item.get("status") == 403
            and item.get("observed_ns", 0) >= started_ns
        ):
            first.setdefault(source, item)
    upper_ms = (max(i["observed_ns"] for i in first.values()) - started_ns) / 1e6 if first else None
    checks = {
        "two_ready_replicas": len(replicas) == 2,
        "real_403_from_both_replicas": set(first) == replicas,
    }
    if limit is not None:
        checks["observed_within_bound"] = upper_ms is not None and upper_ms <= limit * 1000
    return {
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "evidence": {
            "first_by_replica": first,
            "both_replicas_upper_ms": upper_ms,
            "limit_ms": limit * 1000 if limit is not None else None,
        },
    }


def epoch_ns(value: str) -> int:
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(?:Z|\+00:00)", value)
    if not match:
        raise ValueError("timestamp lacks UTC subsecond precision")
    seconds = int(datetime.fromisoformat(match[1]).replace(tzinfo=UTC).timestamp())
    return seconds * 1_000_000_000 + int((match[2] or "").ljust(9, "0"))


def stop_timing(stops, items) -> dict:
    valid_stops = []
    for stop in stops:
        try:
            stamp = epoch_ns(stop["time"])
        except (KeyError, ValueError, TypeError):
            continue
        if stop.get("source_pod"):
            valid_stops.append((stamp, stop))
    # A later idempotent stop must never reset the session's one-second clock.
    anchor = min(valid_stops, key=lambda s: s[0]) if valid_stops else None
    candidates = []
    for item in items if anchor else []:
        if (
            item.get("source_pod") != anchor[1]["source_pod"]
            or item.get("status") != 403
            or item.get("reason") != "deny.session_inactive"
        ):
            continue
        try:
            delta = epoch_ns(item["ts"]) - anchor[0]
        except (KeyError, ValueError, TypeError):
            continue
        if delta >= 0:
            candidates.append((delta, item))
    earliest = min(candidates, key=lambda c: c[0]) if candidates else None
    # Decision timestamps are millisecond-truncated; add one millisecond so this is
    # an upper bound on the same replica's actual event interval.
    upper_ms = earliest[0] / 1e6 + 1 if earliest else None
    return {
        "result": "pass" if upper_ms is not None and upper_ms <= 1000 else "fail",
        "checks": {
            "same_replica_stop_event_and_http_403": earliest is not None,
            "stop_to_403_under_one_second": upper_ms is not None and upper_ms <= 1000,
        },
        "evidence": {
            "same_replica_stop_to_403_ms": earliest[0] / 1e6 if earliest else None,
            "same_replica_upper_ms": upper_ms,
            "stop_events": stops,
            "stop": anchor[1] if anchor else None,
            "denial": earliest[1] if earliest else None,
        },
    }


class LiveEvidence:
    """Read-only log sampling. Observation time includes API/log delay, so bounds are conservative."""

    def __init__(self, core, session, pod, marker):
        self.core, self.session, self.pod, self.marker = core, session, pod, marker
        self.started = time.monotonic()
        self.aps, self.gateway, self.stops = {}, {}, {}
        self.replicas, self.sources = {}, []

    def discover(self):
        from runner.conformance import (
            APS_CONTAINER,
            APS_LABEL,
            APS_NAMESPACE,
            GATEWAY_LABEL,
            GATEWAY_NAMESPACE,
        )

        aps = self.core.list_namespaced_pod(
            APS_NAMESPACE, label_selector=APS_LABEL, _request_timeout=5
        ).items
        ready_pods = [
            p
            for p in aps
            if not p.metadata.deletion_timestamp
            and p.status.phase == "Running"
            and any(c.type == "Ready" and c.status == "True" for c in p.status.conditions or [])
        ]
        if len(ready_pods) != 2 or len(aps) != 2:
            raise ValueError("revocation qualification requires exactly two Ready APS replicas")
        self.replicas = {p.metadata.name: p.metadata.uid for p in ready_pods}
        self.sources = [(APS_NAMESPACE, p.metadata.name, APS_CONTAINER, "aps") for p in ready_pods]
        gateways = self.core.list_namespaced_pod(
            GATEWAY_NAMESPACE, label_selector=GATEWAY_LABEL, _request_timeout=5
        ).items
        if not gateways:
            raise ValueError("no gateway log source")
        self.sources += [(GATEWAY_NAMESPACE, p.metadata.name, None, "gateway") for p in gateways]

    def ingest_aps(self, source, text, observed_ns):
        for raw in text.splitlines():
            try:
                doc = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(doc, dict) or doc.get("session_id") != self.session:
                continue
            if doc.get("msg") == "session stopped":
                self.stops.setdefault(
                    (source, doc.get("time")),
                    {"source_pod": source, "time": doc.get("time"), "observed_ns": observed_ns},
                )
            if (
                doc.get("pod") != self.pod
                or not str(doc.get("trace_id", "")).startswith(self.marker)
                or doc.get("route") != "mcp"
                or doc.get("backend") != "kubernetes"
                or not doc.get("decision_id")
            ):
                continue
            fields = (
                "decision_id",
                "ts",
                "reason",
                "token_jti",
                "trace_id",
                "session_id",
                "pod",
                "generation",
            )
            item = {k: doc.get(k) for k in fields}
            item.update(source_pod=source, observed_ns=observed_ns)
            self.aps.setdefault(item["decision_id"], item)
            if len(self.aps) > 10000:
                raise ValueError("revocation evidence exceeds bounded request count")

    def ingest_gateway(self, text, observed_ns):
        from runner.conformance import parse_gateway_log

        for doc in parse_gateway_log(text):
            key = doc.get("agent.decision_id")
            if (
                key
                and doc.get("agent.session_id") == self.session
                and doc.get("http.path") == "/kubernetes/mcp"
            ):
                try:
                    status = int(doc["http.status"])
                except (ValueError, KeyError):
                    continue
                self.gateway.setdefault(key, {"status": status, "observed_ns": observed_ns})

    def items(self):
        return [
            {
                **value,
                "status": self.gateway[key]["status"],
                "observed_ns": max(value["observed_ns"], self.gateway[key]["observed_ns"]),
            }
            for key, value in self.aps.items()
            if key in self.gateway
        ]

    async def sample(self):
        async def read(source):
            ns, name, container, kind = source
            kw = {"container": container} if container else {}
            text = await asyncio.to_thread(
                self.core.read_namespaced_pod_log,
                name,
                ns,
                since_seconds=int(time.monotonic() - self.started) + 5,
                _request_timeout=5,
                **kw,
            )
            now = time.monotonic_ns()
            if kind == "aps":
                self.ingest_aps(name, text, now)
            else:
                self.ingest_gateway(text, now)

        await asyncio.gather(*(read(s) for s in self.sources))

    async def wait(self, predicate, timeout=30):
        until = time.monotonic() + timeout
        while True:
            await self.sample()
            if predicate(self.items()):
                return
            if time.monotonic() >= until:
                raise TimeoutError("required live gateway/APS evidence was not observed")
            await asyncio.sleep(0.05)

    def allowed(self, after_ns=0):
        return {
            i["source_pod"]
            for i in self.items()
            if i["status"] == 200 and i["reason"] == "allow" and i["observed_ns"] >= after_ns
        } == set(self.replicas)


class ActiveProbe:
    def __init__(self, client, ops, release):
        self.client, self.ops, self.release = client, ops, release
        self.marker = uuid.uuid4().hex[:16]
        self.inp = SessionInput(
            ops.tenant,
            FIXTURE_AGENT,
            release,
            str(uuid.uuid4()),
            None,
            {"tool": "probe_revocation", "args": {"marker": self.marker, "duration": "180"}},
        )
        self.handle = self.result_task = self.pod = self.evidence = None
        self.cleaned = False
        self.cleanup = {}
        self.closed_status = None

    async def __aenter__(self):
        from runner.activities import pod_name

        self.handle = await submit(self.client, self.inp)
        self.result_task = asyncio.create_task(self.handle.result())
        try:
            until = time.monotonic() + 180
            while self.pod is None:
                for pod in await asyncio.to_thread(self.ops.pods, self.inp.session_id):
                    if (
                        pod["metadata"]["name"] == pod_name(self.inp.session_id, 1)
                        and pod.get("status", {}).get("phase") == "Running"
                    ):
                        self.pod = pod
                        break
                if self.result_task.done() or time.monotonic() >= until:
                    raise TimeoutError("no running revocation sandbox")
                if self.pod is None:
                    await asyncio.sleep(0.1)
            await self.assert_live()
            self.evidence = LiveEvidence(
                self.ops.core, self.inp.session_id, self.pod["metadata"]["name"], self.marker
            )
            await asyncio.to_thread(self.evidence.discover)
            await self.evidence.wait(lambda _: self.evidence.allowed(), timeout=90)
            return self
        except BaseException:
            await settled(self.close())
            raise

    async def assert_live(self):
        pods = await asyncio.to_thread(self.ops.pods, self.inp.session_id)
        if len(pods) != 1:
            raise ValueError("revocation session must have exactly one sandbox")
        p = pods[0]
        meta, labels = p["metadata"], p["metadata"].get("labels", {})
        attested = meta.get("annotations", {}).get(LABEL + "netpol-attested", "").split(";")[0]
        try:
            age = time.time_ns() - epoch_ns(attested)
        except (ValueError, TypeError):
            age = -1
        if (
            meta.get("uid") != self.pod["metadata"].get("uid")
            or meta.get("deletionTimestamp")
            or p.get("status", {}).get("phase") != "Running"
            or labels.get(LABEL + "agent") != FIXTURE_AGENT
            or labels.get(LABEL + "release") != self.release
            or labels.get(LABEL + "generation") != "1"
            or not 0 <= age < 60_000_000_000
        ):
            raise ValueError("sandbox identity/liveness/attestation precondition failed")
        return {
            "name": meta["name"],
            "uid": meta["uid"],
            "phase": "Running",
            "deletionTimestamp": None,
            "attestation_age_ms": age / 1e6,
            "generation": 1,
        }

    async def close(self):
        from temporalio.client import WorkflowFailureError
        from runner.workflow import CleanupReconcilerWorkflow
        from runner.worker import cleanup_queue

        if self.handle is None:
            return
        if not self.result_task.done():
            await self.handle.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(self.result_task), 180)
        except WorkflowFailureError:
            pass
        self.closed_status = (await self.handle.describe()).status.name
        if await asyncio.to_thread(self.ops.pods, self.inp.session_id):
            # The injected DELETE denial is non-retryable in normal cleanup. Its policy
            # is already inert before this context exits; use the existing trusted janitor.
            self.cleanup = await self.client.execute_workflow(
                CleanupReconcilerWorkflow.run,
                self.ops.tenant,
                id="conformance-cleanup-" + self.inp.session_id,
                task_queue=cleanup_queue(self.ops.tenant),
                execution_timeout=timedelta(minutes=5),
            )
        self.cleaned = not await asyncio.to_thread(self.ops.pods, self.inp.session_id)
        if not self.cleaned:
            raise TimeoutError("revocation sandbox deletion not confirmed")

    async def __aexit__(self, *_):
        await settled(self.close())


class RevocationHarness:
    def __init__(self, ops, client, base_harness, release, *, allow_mutations=False):
        self.ops, self.client, self.base_harness, self.release = ops, client, base_harness, release
        self.allow_mutations = allow_mutations

    async def _deny(self, active, start, reason, limit):
        ev = active.evidence
        await ev.wait(
            lambda items: {
                i["source_pod"]
                for i in items
                if i["reason"] == reason and i["status"] == 403 and i["observed_ns"] >= start
            }
            == set(ev.replicas),
            timeout=15,
        )
        return denial_evidence(ev.items(), set(ev.replicas), start, reason, limit)

    async def _cancel(self, active, result):
        meta = active.pod["metadata"]
        async with deletion_hold(self.ops, meta["name"], meta["uid"]) as hold:
            # Confirm the active exact rule reached the API; the named denial event below
            # proves the admission engine actually enforced it (readiness alone cannot).
            await asyncio.sleep(1)
            before = await active.assert_live()
            start = time.monotonic_ns()
            cancel_epoch = time.time_ns()
            await active.handle.cancel()
            # Cancellation scheduling and log delivery are reported separately. T15's
            # one-second bound starts at the actual APS stop, not the cancel request.
            deny = await self._deny(active, start, "deny.session_inactive", None)
            try:
                await active.evidence.wait(
                    lambda items: stop_timing(list(active.evidence.stops.values()), items)[
                        "checks"
                    ]["same_replica_stop_event_and_http_403"],
                    timeout=10,
                )
            except TimeoutError:
                # Keep the unmatched stop records and fail the timing gates below.
                # A truncated decision earlier than the stop is never accepted;
                # continue sampling for an unambiguously later same-replica response.
                pass
            stopped = stop_timing(list(active.evidence.stops.values()), active.evidence.items())
            after = await active.assert_live()
            event = None
            until = time.monotonic() + 30
            while event is None and time.monotonic() < until:
                events = await asyncio.to_thread(self.ops.events)
                event = deletion_event(events, meta["name"], meta["uid"], hold["policy_uid"])
                if event is None:
                    await asyncio.sleep(0.1)
            result["checks"] = {
                **deny["checks"],
                **stopped["checks"],
                "named_delete_rule_enforced": event is not None,
                "same_running_pod_before_and_after_stop": before["uid"] == after["uid"],
            }
            result["evidence"] = {
                "cancel_request_epoch_ns": cancel_epoch,
                "cancel_request_monotonic_ns": start,
                "cancel_to_403": deny["evidence"],
                "stop_to_403": stopped["evidence"],
                "deletion_denial": event,
                "before": before,
                "after": after,
            }
        result["checks"]["delete_policy_restored"] = hold["restored"]
        result["result"] = "pass" if all(result["checks"].values()) else "fail"
        return result

    async def _round(self, test, number, partial):
        fixture = AgentFixture(self.ops, self.release)
        active = ActiveProbe(self.client, self.ops, self.release)
        try:
            partial.update(await self._round_body(test, number, fixture, active, partial["cases"]))
        except Exception as error:
            partial.update(result="error", error=type(error).__name__)
            raise
        finally:
            partial["checks"].update(
                fixture_restored=fixture.restored, sandbox_deleted=active.cleaned
            )
        return partial

    async def _round_body(self, test, number, fixture, active, cases):
        async with fixture:
            invariant = await asyncio.to_thread(self.ops.invariant, self.release)
            async with active:
                evidence = active.evidence
                spec = copy.deepcopy(fixture.expected)
                if test == "T15":
                    spec["enabled"] = False
                else:
                    entries = spec["capabilities"]["mcp"]
                    selected = next(e for e in entries if e["backend"] == "kubernetes")
                    if "pods_get" in selected["tools"]:
                        raise ValueError("drift fixture already grants added tool")
                    selected["tools"].append("pods_get")
                timing = await fixture.change(spec)
                if test == "T20":
                    rel = await asyncio.to_thread(
                        self.ops.wait_release,
                        self.release,
                        lambda obj: obj.get("status", {}).get("policyDrift") is True
                        and obj.get("status", {}).get("ready") is False,
                    )
                    upper = (time.monotonic_ns() - timing["request_start_ns"]) / 1e6
                    unchanged = (
                        await asyncio.to_thread(self.ops.invariant, self.release) == invariant
                    )
                    cases.append(
                        {
                            "case": "policy_drift",
                            "result": "pass" if upper <= 10000 and unchanged else "fail",
                            "checks": {
                                "drift_within_ten_seconds": upper <= 10000,
                                "approved_policy_unchanged": unchanged,
                            },
                            "evidence": {
                                "observed_upper_ms": upper,
                                "policyDrift": rel["status"]["policyDrift"],
                                "ready": rel["status"]["ready"],
                                "invariant": invariant,
                            },
                        }
                    )
                denied = await self._deny(
                    active,
                    timing["request_start_ns"],
                    "deny.release_unapproved",
                    5 if test == "T15" else 30,
                )
                cases.append(
                    {
                        "case": "disabled" if test == "T15" else "active_drift_denial",
                        **denied,
                        "mutation": timing,
                    }
                )
                await active.assert_live()
                if test == "T20":
                    sessions = AdmissionSessions(self.client, self.ops, timeout=120)
                    cases.append(
                        {
                            "case": "new_session_preflight",
                            **evaluate_preflight(
                                await sessions.refusal(FIXTURE_AGENT, self.release)
                            ),
                        }
                    )
                    rel = await asyncio.to_thread(self.ops.release, self.release)
                    cases.append(
                        {
                            "case": "immutable_image",
                            **await asyncio.to_thread(self.ops.immutable_image, rel),
                        }
                    )
                restore_start = time.monotonic_ns()
                await settled(fixture.restore())
                await evidence.wait(lambda _: evidence.allowed(restore_start), timeout=30)
                cases.append(
                    {
                        "case": "same_session_reenabled",
                        "result": "pass",
                        "checks": {"both_replicas_allow_after_restore": True},
                    }
                )
                if test == "T15":
                    cancel_case = {"case": "workflow_cancel", "result": "error", "checks": {}}
                    cases.append(cancel_case)
                    await self._cancel(active, cancel_case)
                replicas = dict(evidence.replicas)
                await asyncio.to_thread(evidence.discover)
                same_replicas = evidence.replicas == replicas
                token_ids = {i["token_jti"] for i in evidence.items()}
            invariant_restored = (
                await asyncio.to_thread(self.ops.invariant, self.release) == invariant
            )
        checks = {
            "fixture_restored": fixture.restored,
            "sandbox_deleted": active.cleaned,
            "policy_and_templates_restored": invariant_restored,
            "same_two_aps_replicas": same_replicas,
            "one_probe_token": len(token_ids) == 1 and None not in token_ids,
        }
        if test == "T15":
            checks["workflow_closed_cancelled"] = active.closed_status == "CANCELED"
        return {
            "round": number,
            "result": "pass"
            if all(checks.values()) and all(c["result"] == "pass" for c in cases)
            else "fail",
            "checks": checks,
            "cases": cases,
            "evidence": {
                "session_id": active.inp.session_id,
                "pod": active.pod["metadata"]["name"],
                "pod_uid": active.pod["metadata"]["uid"],
                "aps_replicas": replicas,
                "token_jti": sorted(token_ids),
                "cleanup": active.cleanup,
                "workflow_closed_status": active.closed_status,
            },
        }

    async def _run(self, test):
        rounds = []
        report = {
            "id": test,
            "name": "active-session revocation" if test == "T15" else "active release policy drift",
            "result": "error",
            "checks": {"fixture_restored": False, "cleanup_confirmed": False},
            "rounds": rounds,
        }
        try:
            if not self.allow_mutations or not self.release:
                raise ValueError(
                    "T15/T20 require --allow-revocation-mutations and a conformance-b release"
                )
            rel = await asyncio.to_thread(self.ops.release, self.release)
            if rel["spec"]["agent"] != FIXTURE_AGENT or not ready(rel):
                raise ValueError("revocation fixture must be signed, approved, Ready conformance-b")
            for number in range(1, 3 if test == "T15" else 2):
                partial = {"round": number, "result": "error", "cases": [], "checks": {}}
                rounds.append(partial)
                await self._round(test, number, partial)
            from runner.conformance import t4_case

            positive = await self.base_harness.run_session(t4_case(), FIXTURE_AGENT, self.release)
            report["checks"] = {
                "fixture_restored": all(x["checks"]["fixture_restored"] for x in rounds),
                "cleanup_confirmed": all(x["checks"]["sandbox_deleted"] for x in rounds),
                "fresh_session_completed_after_restoration": positive.outcome == "COMPLETED",
            }
            if test == "T15":
                report["checks"]["warm_repeat_same_replicas"] = (
                    rounds[0]["evidence"]["aps_replicas"] == rounds[1]["evidence"]["aps_replicas"]
                )
            report["restored_session"] = {
                "session_id": positive.session_id,
                "outcome": positive.outcome,
            }
            report["result"] = (
                "pass"
                if all(report["checks"].values()) and all(x["result"] == "pass" for x in rounds)
                else "fail"
            )
        except Exception as error:
            report.update(result="error", error=type(error).__name__)
        return report

    async def t15(self):
        return await self._run("T15")

    async def t20(self):
        return await self._run("T20")
