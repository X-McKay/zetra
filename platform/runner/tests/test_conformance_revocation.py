"""A live revocation verdict requires real HTTP responses and restorable exact fixtures."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from runner import conformance_revocation as r
from runner.conformance_admission import FixtureConflict


def test_hold_is_exact_name_uid_agent_and_delete_only():
    rule = r.hold_rule("s-0123456789abcdef-g1", "ba443cb1-ce1e-4001-b6a9-709fe43c3459")
    assert rule["match"]["any"][0]["resources"] == {
        "kinds": ["Pod"],
        "operations": ["DELETE"],
        "names": ["s-0123456789abcdef-g1"],
    }
    conditions = rule["validate"]["deny"]["conditions"]["all"]
    assert [c["value"] for c in conditions] == [
        "ba443cb1-ce1e-4001-b6a9-709fe43c3459",
        "conformance-b",
    ]
    assert rule["validate"]["failureAction"] == "Enforce"


@pytest.mark.parametrize(
    "name,uid", [("*", "uuid"), ("other-pod", "uuid"), ("s-0123456789abcdef-g1", "")]
)
def test_hold_rejects_unbounded_or_unidentifiable_target(name, uid):
    with pytest.raises(ValueError):
        r.hold_rule(name, uid)


def policy():
    return {
        "apiVersion": "kyverno.io/v1",
        "kind": "Policy",
        "metadata": {
            "name": r.HOLD_POLICY,
            "namespace": "tenant-test",
            "uid": "policy-uid",
            "resourceVersion": "1",
            "annotations": {"other": "keep"},
        },
        "spec": {"background": False, "rules": [r.hold_rule()], "failurePolicy": "Fail"},
    }


class PolicyStore:
    def __init__(self):
        self.value = policy()
        self.fail_after_write = False

    def policy(self):
        return copy.deepcopy(self.value)

    def patch_rule(self, current, rule):
        if current != self.value:
            raise FixtureConflict("concurrent edit")
        self.value["spec"]["rules"] = [copy.deepcopy(rule)]
        self.value["metadata"]["resourceVersion"] = str(
            int(self.value["metadata"]["resourceVersion"]) + 1
        )
        if self.fail_after_write:
            self.fail_after_write = False
            raise OSError("lost response")
        return self.policy()


class AgentStore:
    def __init__(self):
        self.value = {
            "metadata": {
                "name": "conformance-b",
                "uid": "original-agent",
                "annotations": {"other": "keep"},
            },
            "spec": {"capabilities": {"mcp": [{"tools": ["pods_list_in_namespace"]}]}},
        }
        self.fail_after_write = False

    def object(self, *_):
        return copy.deepcopy(self.value)

    def agent_pods(self):
        return []

    def patch_agent(self, current, spec=None, annotation=...):
        if current != self.value:
            raise FixtureConflict("concurrent edit")
        if spec is not None:
            self.value["spec"] = copy.deepcopy(spec)
        if annotation is None:
            self.value["metadata"]["annotations"].pop(r.RECONCILE, None)
        elif annotation is not ...:
            self.value["metadata"]["annotations"][r.RECONCILE] = annotation
        if self.fail_after_write:
            self.fail_after_write = False
            raise OSError("lost response")
        return self.object()

    def wait_release(self, *_):
        return {}


@pytest.mark.parametrize("lost", [False, True])
async def test_agent_fixture_restores_omitted_enabled_and_flux_annotation_after_error(lost):
    store = AgentStore()
    original = copy.deepcopy(store.value)
    with pytest.raises((RuntimeError, OSError)):
        async with r.AgentFixture(store, "signed") as fixture:
            store.fail_after_write = lost
            await fixture.change({**fixture.expected, "enabled": False})
            raise RuntimeError("test failed")
    assert store.value == original


async def test_agent_fixture_refuses_concurrent_spec_changes():
    store = AgentStore()
    with pytest.raises(FixtureConflict):
        async with r.AgentFixture(store, "signed") as fixture:
            await fixture.change({**fixture.expected, "enabled": False})
            store.value["spec"]["operator"] = "preserve"
    assert store.value["spec"]["operator"] == "preserve"


@pytest.mark.parametrize("operation", ["change", "restore", "finish"])
async def test_agent_fixture_never_mutates_replacement_with_identical_spec(operation):
    store = AgentStore()
    fixture = await r.AgentFixture(store, "signed").__aenter__()
    await fixture.change({**fixture.expected, "enabled": False})
    store.value["metadata"]["uid"] = "replacement-agent"
    replacement = copy.deepcopy(store.value)
    with pytest.raises(FixtureConflict):
        if operation == "change":
            await fixture.change({**fixture.expected, "enabled": True})
        else:
            await getattr(fixture, operation)()
    assert store.value == replacement


async def test_agent_fixture_rechecks_uid_before_restoring_flux_annotation():
    store = AgentStore()
    fixture = await r.AgentFixture(store, "signed").__aenter__()

    def replace_after_ready(*_):
        store.value["metadata"]["uid"] = "replacement-agent"

    store.wait_release = replace_after_ready
    with pytest.raises(FixtureConflict):
        await fixture.finish()
    assert store.value["metadata"]["uid"] == "replacement-agent"
    assert store.value["metadata"]["annotations"][r.RECONCILE] == "disabled"


async def test_live_mutation_tests_reject_missing_opt_in_before_any_api_access():
    harness = r.RevocationHarness(None, None, None, "signed", allow_mutations=False)
    result = await harness.t15()
    assert result["result"] == "error" and result["error"] == "ValueError"
    assert result["checks"]["fixture_restored"] is False


def test_cli_requires_explicit_revocation_fixture_and_mutation_flag():
    from runner.conformance import parse_args

    base = ["--release", "primary", "--tests", "T15,T20"]
    with pytest.raises(SystemExit):
        parse_args(base)
    args = parse_args(
        base + ["--revocation-fixture-release", "signed-b", "--allow-revocation-mutations"]
    )
    assert args.tests == ["T15", "T20"] and args.revocation_fixture_release == "signed-b"


@pytest.mark.parametrize("lost_response", [False, True])
async def test_hold_restores_inert_rule_even_after_lost_patch_response(lost_response):
    store = PolicyStore()
    store.fail_after_write = lost_response
    try:
        async with r.deletion_hold(
            store, "s-0123456789abcdef-g1", "ba443cb1-ce1e-4001-b6a9-709fe43c3459"
        ):
            raise RuntimeError("test abort")
    except (RuntimeError, OSError):
        pass
    assert store.value["spec"]["rules"] == [r.hold_rule()]
    assert store.value["metadata"]["annotations"] == {"other": "keep"}
    assert store.value["spec"]["failurePolicy"] == "Fail"


async def test_hold_never_overwrites_concurrent_rule_edit():
    store = PolicyStore()
    with pytest.raises(FixtureConflict):
        async with r.deletion_hold(
            store, "s-0123456789abcdef-g1", "ba443cb1-ce1e-4001-b6a9-709fe43c3459"
        ):
            store.value["spec"]["rules"][0]["validate"]["message"] = "operator edit"
    assert store.value["spec"]["rules"][0]["validate"]["message"] == "operator edit"


async def test_hold_restores_after_harness_task_cancellation():
    store = PolicyStore()
    entered = asyncio.Event()

    async def run():
        async with r.deletion_hold(
            store, "s-0123456789abcdef-g1", "ba443cb1-ce1e-4001-b6a9-709fe43c3459"
        ):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.value["spec"]["rules"] == [r.hold_rule()]


async def test_hold_refuses_replaced_policy_object():
    store = PolicyStore()
    store.value["metadata"]["uid"] = "original"
    with pytest.raises(FixtureConflict):
        async with r.deletion_hold(
            store, "s-0123456789abcdef-g1", "ba443cb1-ce1e-4001-b6a9-709fe43c3459"
        ):
            store.value["metadata"]["uid"] = "replacement"
    assert store.value["spec"]["rules"] != [r.hold_rule()]


@pytest.mark.parametrize("concurrent_change", ["uid", "spec"])
async def test_hold_final_receipt_rejects_post_patch_replacement_or_spec_change(concurrent_change):
    class ChangedAfterRestore(PolicyStore):
        def patch_rule(self, current, rule):
            result = super().patch_rule(current, rule)
            if rule == r.hold_rule():
                if concurrent_change == "uid":
                    self.value["metadata"]["uid"] = "replacement-policy"
                else:
                    self.value["spec"]["background"] = True
            return result

    store = ChangedAfterRestore()
    with pytest.raises(FixtureConflict):
        async with r.deletion_hold(
            store, "s-0123456789abcdef-g1", "ba443cb1-ce1e-4001-b6a9-709fe43c3459"
        ) as receipt:
            assert receipt["policy_uid"] == "policy-uid"
    assert receipt["restored"] is False
    assert store.value["metadata"]["uid"] == (
        "replacement-policy" if concurrent_change == "uid" else "policy-uid"
    )
    assert store.value["spec"]["background"] is (concurrent_change == "spec")


async def test_cleanup_uses_trusted_reconciler_and_confirms_absence():
    pods = [{"metadata": {"name": "held"}}]
    ops = SimpleNamespace(tenant="tenant-test", pods=lambda session: copy.deepcopy(pods))
    reconciled = []

    class Temporal:
        async def execute_workflow(self, workflow, tenant, **kwargs):
            reconciled.append((tenant, kwargs["task_queue"]))
            pods.clear()
            return {"orphans_deleted": 1, "orphans_unconfirmed": 0}

    active = r.ActiveProbe(Temporal(), ops, "release")
    active.result_task = asyncio.create_task(asyncio.sleep(0))
    await active.result_task

    async def describe():
        return SimpleNamespace(status=SimpleNamespace(name="CANCELED"))

    active.handle = SimpleNamespace(describe=describe)
    await active.close()
    assert active.cleaned and pods == []
    assert reconciled == [("tenant-test", "cleanup-tenant-test")]
    assert active.cleanup["orphans_deleted"] == 1
    assert active.closed_status == "CANCELED"


async def test_partial_round_evidence_survives_failure(monkeypatch):
    ops = SimpleNamespace(tenant="tenant-test")
    harness = r.RevocationHarness(ops, None, None, "signed", allow_mutations=True)

    async def fail(test, number, fixture, active, cases):
        cases.append({"case": "disabled", "result": "pass", "evidence": {"observed_upper_ms": 500}})
        fixture.restored, active.cleaned = True, True
        raise TimeoutError("later cancellation evidence missing")

    monkeypatch.setattr(harness, "_round_body", fail)
    partial = {"round": 1, "result": "error", "cases": [], "checks": {}}
    with pytest.raises(TimeoutError):
        await harness._round("T15", 1, partial)
    assert partial["cases"][0]["evidence"]["observed_upper_ms"] == 500
    assert partial["checks"] == {"fixture_restored": True, "sandbox_deleted": True}
    assert partial["result"] == "error"


def test_merge_patch_removes_fields_added_by_fixture_without_changing_siblings():
    assert r.merge_patch(
        {"enabled": False, "nested": {"keep": 1, "added": 2}}, {"nested": {"keep": 1}}
    ) == {
        "enabled": None,
        "nested": {"added": None},
    }


def test_immutability_requires_crd_image_rule_not_rbac_or_shape_error():
    from kubernetes.client.exceptions import ApiException

    error = ApiException(status=422)
    error.body = json.dumps(
        {
            "message": "AgentRelease invalid: spec: image is immutable",
            "details": {"causes": [{"field": "spec", "message": "image is immutable"}]},
        }
    )
    assert r.immutable_image_result(error)["result"] == "pass"
    for status, message in [
        (403, "image is immutable"),
        (422, "image must match pattern"),
        (500, "image is immutable"),
    ]:
        error.status, error.body = status, json.dumps({"message": message})
        assert r.immutable_image_result(error)["result"] != "pass"
    assert r.immutable_image_result(None)["result"] == "fail"


def test_nonblocked_resource_violation_event_is_not_delete_enforcement():
    event = {
        "reason": "PolicyViolation",
        "involvedObject": {"name": "s-abc", "uid": "uid1"},
        "message": "policy conformance-revocation-delete-hold/hold-conformance-session fail: held",
    }
    assert r.deletion_event([event], "s-abc", "uid1", "policy-uid") is None
    assert r.deletion_event([event], "s-abc", "uid2", "policy-uid") is None
    event["message"] = "policy unrelated/hold-conformance-session fail: held"
    assert r.deletion_event([event], "s-abc", "uid1", "policy-uid") is None


def test_blocked_kyverno_event_is_on_policy_with_related_pod_uid():
    event = {
        "reason": "PolicyViolation",
        "action": "Resource Blocked",
        "involvedObject": {
            "kind": "Policy",
            "name": "conformance-revocation-delete-hold",
            "uid": "policy-uid",
        },
        "related": {"kind": "Pod", "name": "s-abc", "uid": "pod-uid"},
        "message": "Pod tenant-test/s-abc: [hold-conformance-session] fail (blocked); held",
    }
    assert r.deletion_event([event], "s-abc", "pod-uid", "policy-uid") is not None
    assert r.deletion_event([event], "s-abc", "other-uid", "policy-uid") is None
    event["action"] = "Resource Passed"
    assert r.deletion_event([event], "s-abc", "pod-uid", "policy-uid") is None


@pytest.mark.parametrize("policy_uid", ["replacement-policy", None])
def test_blocked_event_requires_original_policy_uid(policy_uid):
    event = {
        "reason": "PolicyViolation",
        "action": "Resource Blocked",
        "involvedObject": {"kind": "Policy", "name": r.HOLD_POLICY, "uid": policy_uid},
        "related": {"kind": "Pod", "name": "s-abc", "uid": "pod-uid"},
        "message": "Pod tenant-test/s-abc: [hold-conformance-session] fail (blocked); held",
    }
    assert r.deletion_event([event], "s-abc", "pod-uid", "policy-uid") is None


def test_live_evidence_joins_real_gateway_status_with_exact_session_trace_and_source():
    evidence = r.LiveEvidence(None, "session-1", "s-pod", "1234567890abcdef")
    decision = {
        "decision_id": "d1",
        "pod": "s-pod",
        "session_id": "session-1",
        "trace_id": "1234567890abcdef0000000000000001",
        "token_jti": "j1",
        "reason": "deny.session_inactive",
        "route": "mcp",
        "backend": "kubernetes",
        "ts": "2026-11-06T19:00:00.200Z",
    }
    evidence.ingest_aps("aps-1", json.dumps(decision), 1_100_000_000)
    assert evidence.items() == []
    evidence.ingest_gateway(
        "http.path=/kubernetes/mcp http.status=403 agent.session_id=session-1 agent.decision_id=d1",
        1_200_000_000,
    )
    item = evidence.items()[0]
    assert item["status"] == 403 and item["source_pod"] == "aps-1"
    assert item["observed_ns"] == 1_200_000_000
    evidence.ingest_aps(
        "aps-2",
        json.dumps({**decision, "decision_id": "wrong", "session_id": "other"}),
        1_300_000_000,
    )
    assert len(evidence.items()) == 1


def test_stop_timing_uses_same_replica_clock_and_positive_subsecond_delta():
    stop = {"time": "2026-11-06T19:00:00.123456789Z", "source_pod": "aps-1"}
    decisions = [
        {
            "ts": "2026-11-06T19:00:00.200Z",
            "source_pod": "aps-1",
            "reason": "deny.session_inactive",
            "status": 403,
        }
    ]
    result = r.stop_timing([stop], decisions)
    assert result["result"] == "pass"
    assert result["evidence"]["same_replica_stop_to_403_ms"] == pytest.approx(76.543211)
    decisions[0]["source_pod"] = "aps-2"
    assert r.stop_timing([stop], decisions)["result"] == "fail"


@pytest.mark.parametrize(
    "timestamp", ["2026-11-06T19:00:00.122Z", "2026-11-06T19:00:01.124Z", "bad"]
)
def test_stop_timing_cannot_pass_negative_late_or_malformed_time(timestamp):
    stop = {"time": "2026-11-06T19:00:00.123Z", "source_pod": "aps-1"}
    item = {
        "ts": timestamp,
        "source_pod": "aps-1",
        "reason": "deny.session_inactive",
        "status": 403,
    }
    assert r.stop_timing([stop], [item])["result"] == "fail"


def observations():
    return [
        {
            "decision_id": "a",
            "reason": "deny.release_unapproved",
            "source_pod": "aps-1",
            "status": 403,
            "observed_ns": 4_000_000_000,
        },
        {
            "decision_id": "b",
            "reason": "deny.release_unapproved",
            "source_pod": "aps-2",
            "status": 403,
            "observed_ns": 4_100_000_000,
        },
    ]


def test_timing_uses_observed_upper_bound_and_requires_both_replicas():
    result = r.denial_evidence(
        observations(), {"aps-1", "aps-2"}, 1_000_000_000, "deny.release_unapproved", 5
    )
    assert result["result"] == "pass"
    assert result["evidence"]["both_replicas_upper_ms"] == 3100


@pytest.mark.parametrize(
    "change",
    ["one_replica", "wrong_status", "missing_status", "wrong_reason", "late", "before_mutation"],
)
def test_incomplete_or_wrong_http_evidence_never_passes(change):
    items = observations()
    if change == "one_replica":
        items.pop()
    elif change == "wrong_status":
        items[0]["status"] = 502
    elif change == "missing_status":
        items[0].pop("status")
    elif change == "wrong_reason":
        items[0]["reason"] = "deny.internal"
    elif change == "late":
        items[0]["observed_ns"] = 6_100_000_000
    else:
        items[0]["observed_ns"] = 900_000_000
    assert (
        r.denial_evidence(items, {"aps-1", "aps-2"}, 1_000_000_000, "deny.release_unapproved", 5)[
            "result"
        ]
        != "pass"
    )


def test_cancel_observation_latency_is_informational_but_both_replicas_required():
    items = [{**i, "reason": "deny.session_inactive"} for i in observations()]
    result = r.denial_evidence(
        items, {"aps-1", "aps-2"}, 1_000_000_000, "deny.session_inactive", None
    )
    assert result["result"] == "pass"
    assert "observed_within_bound" not in result["checks"]
    assert result["evidence"]["both_replicas_upper_ms"] == 3100
    assert result["evidence"]["limit_ms"] is None
    assert (
        r.denial_evidence(
            items[:1], {"aps-1", "aps-2"}, 1_000_000_000, "deny.session_inactive", None
        )["result"]
        == "fail"
    )


@pytest.mark.parametrize(
    "later_timestamp, expected",
    [
        ("2026-11-06T17:45:08.600Z", "pass"),
        ("2026-11-06T17:45:09.566Z", "fail"),
        (None, "fail"),
    ],
)
async def test_cancel_waits_for_same_replica_pair_and_gates_from_real_stop(
    monkeypatch, later_timestamp, expected
):
    # Run 19: the first decision's millisecond bin overlaps the nanosecond stop;
    # its negative point delta is ambiguous. A later same-replica pair is required.
    stop = {"time": "2026-11-06T17:45:08.565989092Z", "source_pod": "aps-1"}
    items = [
        {
            "ts": "2026-11-06T17:45:08.565Z",
            "source_pod": "aps-1",
            "reason": "deny.session_inactive",
            "status": 403,
            "observed_ns": 3_500_000_000,
        },
        {
            "ts": "2026-11-06T17:45:08.580Z",
            "source_pod": "aps-2",
            "reason": "deny.session_inactive",
            "status": 403,
            "observed_ns": 3_583_326_000,
        },
    ]

    class Evidence:
        replicas = {"aps-1": "uid-1", "aps-2": "uid-2"}
        stops = {"first": stop}
        waits = 0

        def items(self):
            return items

        async def wait(self, predicate, timeout):
            self.waits += 1
            if self.waits == 1:
                assert predicate(items)  # both actual 403s, after a slow cancel
            else:
                assert not predicate(items)  # a stop log alone cannot finish sampling
                if later_timestamp is None:
                    raise TimeoutError("no unambiguous same-replica pair")
                items.append({**items[0], "ts": later_timestamp, "observed_ns": 3_600_000_000})
                assert predicate(items)  # valid pair, even if it fails the one-second bound

    pod = {
        "metadata": {"name": "s-0123456789abcdef-g1", "uid": "ba443cb1-ce1e-4001-b6a9-709fe43c3459"}
    }
    store = PolicyStore()
    store.events = lambda: [
        {
            "involvedObject": {"kind": "Policy", "name": r.HOLD_POLICY, "uid": "policy-uid"},
            "related": {
                "kind": "Pod",
                "name": pod["metadata"]["name"],
                "uid": "ba443cb1-ce1e-4001-b6a9-709fe43c3459",
            },
            "reason": "PolicyViolation",
            "action": "Resource Blocked",
            "message": f"[{r.HOLD_RULE}] fail (blocked)",
        }
    ]

    async def live():
        return {"uid": "ba443cb1-ce1e-4001-b6a9-709fe43c3459"}

    async def no_op(*args):
        pass

    monkeypatch.setattr(r.time, "monotonic_ns", lambda: 1_000_000_000)
    monkeypatch.setattr(r.asyncio, "sleep", no_op)
    evidence = Evidence()
    active = SimpleNamespace(
        pod=pod, evidence=evidence, assert_live=live, handle=SimpleNamespace(cancel=no_op)
    )
    harness = r.RevocationHarness(store, None, None, "signed", allow_mutations=True)
    result = await harness._cancel(active, {})
    assert evidence.waits == 2
    assert result["result"] == expected
    assert result["checks"]["real_403_from_both_replicas"]
    assert result["checks"]["delete_policy_restored"]
    assert "observed_within_bound" not in result["checks"]
    assert result["evidence"]["cancel_to_403"]["both_replicas_upper_ms"] == 2583.326
    timing = result["evidence"]["stop_to_403"]
    assert timing["stop_events"] == [stop]
    if later_timestamp is None:
        assert timing["same_replica_upper_ms"] is None
        assert not result["checks"]["same_replica_stop_event_and_http_403"]
    else:
        assert timing["same_replica_upper_ms"] == pytest.approx(
            35.010908 if expected == "pass" else 1001.010908
        )


@pytest.mark.parametrize("later_source", ["aps-1", "aps-2"])
def test_later_idempotent_stop_cannot_reset_original_stop_bound(later_source):
    stops = [
        {"time": "2026-11-06T19:00:00.000000000Z", "source_pod": "aps-1"},
        {"time": "2026-11-06T19:00:02.000000000Z", "source_pod": later_source},
    ]
    decisions = [
        {
            "ts": "2026-11-06T19:00:02.100Z",
            "source_pod": later_source,
            "status": 403,
            "reason": "deny.session_inactive",
        }
    ]
    result = r.stop_timing(list(reversed(stops)), decisions)
    assert result["result"] == "fail"
    assert result["evidence"]["stop"] == stops[0]
    if later_source == "aps-1":
        assert result["evidence"]["same_replica_upper_ms"] == 2101
    else:
        assert result["evidence"]["same_replica_upper_ms"] is None


async def test_agent_mutation_receipt_identifies_disabled_field_and_real_readback():
    store = AgentStore()
    store.tenant = "tenant-test"
    store.value["metadata"].update(namespace="tenant-test", resourceVersion="100")
    async with r.AgentFixture(store, "signed") as fixture:
        receipt = await fixture.change({**fixture.expected, "enabled": False})
        assert receipt["target"] == {
            "api_version": "agentplatform.io/v1alpha1",
            "kind": "Agent",
            "namespace": "tenant-test",
            "name": "conformance-b",
            "uid": "original-agent",
        }
        assert receipt["spec_patch"] == {"enabled": False}
        assert receipt["readback"] == {"resource_version": "100", "enabled": False}
        restored = await fixture.restore()
        assert restored["spec_patch"] == {"enabled": None}
        assert restored["readback"]["enabled"] is True
        assert restored["target"] == receipt["target"]


@pytest.mark.parametrize("fault", ["ignored_write", "replacement"])
async def test_agent_change_never_reports_unobserved_or_replacement_mutation(fault):
    store = AgentStore()
    fixture = await r.AgentFixture(store, "signed").__aenter__()
    write = store.patch_agent

    def broken_write(current, spec=None, annotation=...):
        if fault == "replacement":
            write(current, spec, annotation)
            store.value["metadata"]["uid"] = "replacement-agent"
        return store.object()

    store.patch_agent = broken_write
    with pytest.raises(FixtureConflict):
        await fixture.change({**fixture.expected, "enabled": False})
    if fault == "ignored_write":
        store.patch_agent = write
        await fixture.finish()
    else:
        assert store.value["metadata"]["uid"] == "replacement-agent"


async def test_restore_requires_current_agent_readback_not_only_release_ready():
    store = AgentStore()
    fixture = await r.AgentFixture(store, "signed").__aenter__()
    await fixture.change({**fixture.expected, "enabled": False})
    write = store.patch_agent
    store.patch_agent = lambda *_: store.object()
    with pytest.raises(FixtureConflict):
        await fixture.restore()
    store.patch_agent = write
    await fixture.finish()


async def test_t15_actual_api_path_disables_agent_without_release_approval_mutation(monkeypatch):
    class CustomAPI:
        def __init__(self):
            self.agent = {
                "metadata": {
                    "name": "conformance-b",
                    "namespace": "tenant-test",
                    "uid": "agent-uid",
                    "resourceVersion": "1",
                },
                "spec": {"enabled": True, "capabilities": {"mcp": []}},
            }
            self.release = {"spec": {"agent": "conformance-b", "approval": {"approved": True}}}
            self.writes = []

        def get_namespaced_custom_object(self, group, version, namespace, plural, name, **kwargs):
            assert (group, version, namespace) == ("agentplatform.io", "v1alpha1", "tenant-test")
            assert (plural, name) in (("agents", "conformance-b"), ("agentreleases", "signed"))
            return copy.deepcopy(self.agent if plural == "agents" else self.release)

        def patch_namespaced_custom_object(
            self, group, version, namespace, plural, name, body, **kwargs
        ):
            assert (group, version, namespace, plural, name) == (
                "agentplatform.io",
                "v1alpha1",
                "tenant-test",
                "agents",
                "conformance-b",
            )
            assert body["metadata"]["resourceVersion"] == self.agent["metadata"]["resourceVersion"]
            self.writes.append(copy.deepcopy(body))
            if "spec" in body:
                self.agent["spec"].update(body["spec"])
            if "annotations" in body["metadata"]:
                self.agent["metadata"]["annotations"] = {
                    k: v for k, v in body["metadata"]["annotations"].items() if v is not None
                }
            self.agent["metadata"]["resourceVersion"] = str(
                int(self.agent["metadata"]["resourceVersion"]) + 1
            )
            return copy.deepcopy(self.agent)

    custom = CustomAPI()
    core = SimpleNamespace(list_namespaced_pod=lambda *a, **k: SimpleNamespace(items=[]))
    ops = r.RevocationOps(core, custom, "tenant-test")
    ops.invariant = lambda *_: {}
    ops.wait_release = lambda *_: custom.release
    harness = r.RevocationHarness(ops, None, None, "signed", allow_mutations=True)

    class Active:
        evidence = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    class ReachedDenial(Exception):
        pass

    async def deny(probe, start, reason, limit):
        assert custom.agent["spec"]["enabled"] is False
        assert custom.release["spec"]["approval"]["approved"] is True
        assert reason == "deny.release_unapproved" and limit == 5
        raise ReachedDenial

    monkeypatch.setattr(harness, "_deny", deny)
    with pytest.raises(ReachedDenial):
        await harness._round_body("T15", 1, r.AgentFixture(ops, "signed"), Active(), [])
    assert [write["spec"] for write in custom.writes if "spec" in write] == [
        {"enabled": False},
        {"enabled": True},
    ]
    assert custom.agent["spec"]["enabled"] is True
    assert custom.release["spec"]["approval"]["approved"] is True
