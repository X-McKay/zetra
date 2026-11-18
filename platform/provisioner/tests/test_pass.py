import copy
import logging

import pytest

from registry_provisioner.main import main
from registry_provisioner.registry import RegistryError
from tests.fakes import AV, PREFIX, example

NS = "tenant-test"
GEN = PREFIX + "generation"


def run(kube, registry, env):
    return main(["--once"], kube=kube, registry=registry, environ=env)


def results(registry):
    return {p["name"]: p for p in registry.reports[-1]["projections"]}


def one(desired, index=0):
    """The answer narrowed to one of its objects."""
    desired = copy.deepcopy(desired)
    obj = desired["deployments"][0]["objects"][index]
    desired["deployments"][0]["objects"] = [obj]
    return desired, obj


def live_of(obj):
    return copy.deepcopy(obj["document"])


# 1. the table, row by row


def test_namespace_not_listed_is_error(kube, registry, desired, env):
    registry.answer, obj = one(desired)
    obj["namespace"] = obj["document"]["metadata"]["namespace"] = "elsewhere"
    assert run(kube, registry, env) == 0
    assert results(registry)["db-admin"]["result"] == "error"
    assert kube.writes == [("ConfigMap", "provisioner", "registry-provisioner-state")]


def test_other_kind_is_error(kube, registry, desired, env):
    registry.answer, obj = one(desired)
    obj["kind"] = obj["document"]["kind"] = "Secret"
    run(kube, registry, env)
    assert results(registry)["db-admin"]["result"] == "error"
    assert not any(w[0] == "Secret" for w in kube.writes)


def test_flux_inventory_refused_and_observed(kube, registry, desired, env):
    registry.answer, obj = one(desired)
    kube.put_kustomization(ids=[f"{NS}_db-admin_agentplatform.io_Agent"])
    kube.put(live_of(obj))
    run(kube, registry, env)
    assert results(registry)["db-admin"]["result"] == "refused-flux-inventory"
    assert [w for w in kube.writes if w[0] != "ConfigMap"] == []
    assert registry.reports[-1]["objects"][0]["inFluxInventory"] is True


def test_flux_entry_of_another_object_does_not_match(kube, registry, desired, env):
    registry.answer, _ = one(desired)
    kube.put_kustomization(ids=[f"{NS}_db-admin_agentplatform.io_AgentRelease"])
    run(kube, registry, env)
    assert results(registry)["db-admin"]["result"] == "applied"


def test_missing_object_is_applied(kube, registry, desired, env):
    registry.answer, _ = one(desired)
    run(kube, registry, env)
    assert results(registry)["db-admin"]["result"] == "applied"
    assert ("Agent", NS, "db-admin") in kube.writes
    assert set(kube.managers) == {"agent-registry-provisioner"}


def test_live_without_generation_is_adopted(kube, registry, desired, env):
    registry.answer, obj = one(desired)
    live = live_of(obj)
    del live["metadata"]["annotations"]
    kube.put(live)
    run(kube, registry, env)
    got = results(registry)["db-admin"]
    assert got["result"] == "applied" and got["message"].startswith("adopted")
    assert ("Agent", NS, "db-admin") in kube.writes
    assert kube.obj(AV, "Agent", NS, "db-admin")["metadata"]["annotations"][GEN] == "3"


def test_live_newer_is_refused(kube, registry, desired, env):
    registry.answer, obj = one(desired)
    live = live_of(obj)
    live["metadata"]["annotations"][GEN] = "4"
    kube.put(live)
    run(kube, registry, env)
    assert results(registry)["db-admin"]["result"] == "refused-older"
    assert ("Agent", NS, "db-admin") not in kube.writes


def test_live_older_is_applied(kube, registry, desired, env):
    registry.answer, obj = one(desired)
    live = live_of(obj)
    live["metadata"]["annotations"][GEN] = "2"
    live["spec"]["description"] = "old"
    kube.put(live)
    run(kube, registry, env)
    assert results(registry)["db-admin"] == {
        **results(registry)["db-admin"],
        "result": "applied",
        "message": "",
    }
    assert kube.obj(AV, "Agent", NS, "db-admin")["spec"] == obj["document"]["spec"]


def test_equal_is_unchanged_whatever_else_differs(kube, registry, desired, env):
    registry.answer, obj = one(desired)
    live = live_of(obj)
    live["metadata"]["annotations"]["other.io/x"] = "y"
    live["metadata"]["labels"] = {"a": "b"}
    live["status"] = {"ready": True}
    kube.put(live)
    run(kube, registry, env)
    assert results(registry)["db-admin"]["result"] == "unchanged"
    assert ("Agent", NS, "db-admin") not in kube.writes


@pytest.mark.parametrize("change", ["spec", "annotation", "spec-key-removed"])
def test_equal_generation_but_different_is_repaired(kube, registry, desired, env, change):
    registry.answer, obj = one(desired)
    live = live_of(obj)
    if change == "spec":
        live["spec"]["enabled"] = False
    elif change == "annotation":
        live["metadata"]["annotations"][PREFIX + "current-release"] = "other"
    else:
        del live["spec"]["runtime"]
    kube.put(live)
    run(kube, registry, env)
    got = results(registry)["db-admin"]
    assert got["result"] == "applied"
    assert got["message"] == "repaired: the live object had been changed"
    assert kube.obj(AV, "Agent", NS, "db-admin")["spec"] == obj["document"]["spec"]


def test_first_matching_row_wins_flux_over_older(kube, registry, desired, env):
    registry.answer, obj = one(desired)
    live = live_of(obj)
    live["metadata"]["annotations"][GEN] = "9"
    kube.put(live)
    kube.put_kustomization(ids=[f"{NS}_db-admin_agentplatform.io_Agent"])
    run(kube, registry, env)
    assert results(registry)["db-admin"]["result"] == "refused-flux-inventory"


def test_a_mismatched_document_is_error(kube, registry, desired, env):
    registry.answer, obj = one(desired)
    obj["document"]["metadata"]["name"] = "someone-else"
    run(kube, registry, env)
    assert results(registry)["db-admin"]["result"] == "error"
    assert kube.obj(AV, "Agent", NS, "someone-else") is None


# 2. the registry does not answer


@pytest.mark.parametrize(
    "error",
    [
        RegistryError("unreachable"),
        RegistryError("500", status=500, code="internal", retryable=True),
        RegistryError("401", status=401, code="unauthorized"),
    ],
)
def test_no_answer_changes_nothing(kube, registry, env, error):
    kube.set_stored(5)
    registry.desired_error = error
    before = dict(kube.objects)
    assert run(kube, registry, env) != 0
    assert kube.writes == [] and kube.stored() == 5
    assert kube.objects == before and registry.reports == []


# 3. recovery


def test_recovery_writes_nothing_and_still_reports(kube, registry, desired, env):
    kube.put({"apiVersion": AV, "kind": "Agent", "metadata": {"namespace": NS, "name": "x"}})
    registry.answer = {**desired, "mode": "recovery", "generation": 1, "deployments": []}
    assert run(kube, registry, env) == 0
    assert [w for w in kube.writes if w[0] != "ConfigMap"] == []
    report = registry.reports[0]
    assert report["projections"] == []
    assert [o["name"] for o in report["objects"]] == ["x"]
    assert len(report["subjects"]) == 2


def test_recovery_ignores_deployments_it_was_given(kube, registry, desired, env):
    registry.answer = {**desired, "mode": "recovery"}
    run(kube, registry, env)
    assert [w for w in kube.writes if w[0] != "ConfigMap"] == []
    assert registry.reports[0]["projections"] == []


# 4. lastGeneration


def test_generation_stored_before_any_write(kube, registry, env):
    kube.fail_apply_all = False
    kube.fail_apply = {"db-admin", "db-admin-2026-11-07-3e94dc5e"}
    kube.fail_apply |= {"db-admin-2026-11-13-33c8acb2", "db-admin-2026-11-18-aaaaaaaa"}
    assert run(kube, registry, env) == 0
    assert kube.stored() == 3
    assert {p["result"] for p in registry.reports[0]["projections"]} == {"error"}


def test_generation_sent_next_pass_and_never_lowered(kube, registry, desired, env):
    run(kube, registry, env)
    assert registry.desired_calls == [0] and kube.stored() == 3
    registry.answer = {**desired, "generation": 1}
    run(kube, registry, env)
    assert registry.desired_calls == [0, 3] and kube.stored() == 3
    assert registry.reports[-1]["lastGeneration"] == 3
    registry.answer = {**desired, "generation": 7}
    run(kube, registry, env)
    run(kube, registry, env)
    assert registry.desired_calls[-1] == 7 and kube.stored() == 7


def test_state_that_cannot_be_stored_stops_the_pass(kube, registry, env):
    kube.fail_apply = {"registry-provisioner-state"}
    assert run(kube, registry, env) != 0
    assert kube.writes == [] and registry.reports == []


# 5. observations


def test_observations_cover_everything_and_filter_annotations(kube, registry, desired, env):
    stranger = {
        "apiVersion": AV,
        "kind": "AgentRelease",
        "metadata": {
            "namespace": NS,
            "name": "hand-made",
            "annotations": {"kubectl.kubernetes.io/x": "secret-ish", PREFIX + "revision": "9"},
            "labels": {"l": "v"},
            "ownerReferences": [{"name": "o"}],
        },
        "spec": {"agent": "other"},
        "status": {"ready": False},
    }
    kube.put(stranger)
    kube.put({**stranger, "kind": "Agent", "metadata": {"namespace": "unlisted", "name": "z"}})
    registry.answer = {**desired, "deployments": []}
    run(kube, registry, env)
    report = registry.reports[0]
    assert report["complete"] == [
        {"subjectKind": "agent", "namespace": NS},
        {"subjectKind": "agentrelease", "namespace": NS},
    ]
    (obj,) = report["objects"]
    assert obj["name"] == "hand-made" and obj["subjectKind"] == "agentrelease"
    assert obj["inFluxInventory"] is False
    assert set(obj["document"]) == {"metadata", "spec", "status"}
    assert obj["document"]["metadata"]["annotations"] == {PREFIX + "revision": "9"}
    assert set(obj["document"]["metadata"]) == {
        "name",
        "namespace",
        "uid",
        "resourceVersion",
        "annotations",
    }
    assert "secret-ish" not in str(report)
    assert "ownerReferences" not in str(report) and "labels" not in str(report)


def test_every_object_of_the_namespace_is_observed(kube, registry, desired, env):
    run(kube, registry, env)
    names = [o["name"] for o in registry.reports[0]["objects"]]
    assert sorted(names) == sorted(
        ["db-admin", "db-admin-2026-11-07-3e94dc5e", "db-admin-2026-11-13-33c8acb2"]
        + ["db-admin-2026-11-18-aaaaaaaa"]
    )


def test_missing_workload_is_not_present(kube, registry, env):
    run(kube, registry, env)
    subjects = {s["name"]: s for s in registry.reports[0]["subjects"]}
    assert subjects["postgres-admin-mcp"] == {
        "subjectKind": "tool-server",
        "namespace": "mcp",
        "name": "postgres-admin-mcp",
        "present": False,
        "document": {},
    }
    assert subjects["main"]["present"] is False


def test_workload_and_served_name(kube, registry, env):
    kube.put_deployment("mcp", "postgres-admin-mcp", ["img/a@sha256:1"], replicas=2, ready=1)
    kube.put_deployment("vllm", "vllm", ["vllm/vllm-openai:v0.30.0", "side:1"], 1, 1)
    kube.put(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"namespace": "vllm", "name": "model-config"},
            "data": {"LLM_MODEL_NAME": "Qwen3.6-35B-A3B-NVFP4", "OTHER": "x"},
        }
    )
    run(kube, registry, env)
    subjects = {s["name"]: s for s in registry.reports[0]["subjects"]}
    assert subjects["postgres-admin-mcp"]["document"] == {
        "images": ["img/a@sha256:1"],
        "replicas": 2,
        "readyReplicas": 1,
    }
    assert subjects["main"]["document"] == {
        "images": ["vllm/vllm-openai:v0.30.0", "side:1"],
        "replicas": 1,
        "readyReplicas": 1,
        "servedName": "Qwen3.6-35B-A3B-NVFP4",
    }


def test_a_scope_that_cannot_be_listed_is_not_complete(kube, registry, env):
    kube.fail_list = {"AgentRelease"}
    assert run(kube, registry, env) == 0
    assert registry.reports[0]["complete"] == [{"subjectKind": "agent", "namespace": NS}]


# 6. one apply fails


def test_one_failure_does_not_stop_the_others(kube, registry, env):
    kube.fail_apply = {"db-admin-2026-11-13-33c8acb2"}
    assert run(kube, registry, env) == 0
    got = results(registry)
    assert got["db-admin-2026-11-13-33c8acb2"]["result"] == "error"
    assert "refused" in got["db-admin-2026-11-13-33c8acb2"]["message"]
    assert [p["result"] for n, p in got.items() if n != "db-admin-2026-11-13-33c8acb2"] == [
        "applied"
    ] * 3
    assert len(registry.reports) == 1


# 7. the examples


def keys(value):
    if isinstance(value, dict):
        return {k: keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [keys(v) for v in value]
    return None


def test_example_applies_then_is_unchanged(kube, registry, env):
    assert run(kube, registry, env) == 0
    first = registry.reports[0]
    assert [p["result"] for p in first["projections"]] == ["applied"] * 4
    expected = example("observations.json")
    for sent, wanted in zip(first["projections"], expected["projections"], strict=True):
        assert {k: v for k, v in sent.items() if k not in ("result", "message")} == {
            k: v for k, v in wanted.items() if k not in ("result", "message")
        }
    writes = len(kube.writes)
    assert run(kube, registry, env) == 0
    assert [p["result"] for p in registry.reports[1]["projections"]] == ["unchanged"] * 4
    assert len(kube.writes) == writes


def test_report_has_the_keys_of_the_example(kube, registry, env):
    kube.put_deployment("mcp", "postgres-admin-mcp", ["i:1"])
    kube.put_deployment("vllm", "vllm", ["i:2"])
    kube.put(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"namespace": "vllm", "name": "model-config"},
            "data": {"LLM_MODEL_NAME": "m"},
        }
    )
    run(kube, registry, env)
    run(kube, registry, env)
    sent = registry.reports[1]
    wanted = example("observations.json")
    assert keys(sent["projections"]) == keys(wanted["projections"])
    assert keys(sent["complete"]) == keys(wanted["complete"])
    assert keys(sent["subjects"]) == keys(wanted["subjects"])
    assert set(sent) == set(wanted)
    # an applied object has no status yet; the example's Agent shows `status: {}`
    assert keys(sent["objects"][0]) == keys(wanted["objects"][0])
    assert sent["observedAt"].endswith("Z") and sent["lastGeneration"] == 3


# 8. Flux inventory


def test_unreadable_inventory_writes_nothing(kube, registry, env):
    registry.answer["fluxInventories"] = [{"namespace": "flux-system", "name": "gone"}]
    assert run(kube, registry, env) == 0
    assert [w for w in kube.writes if w[0] != "ConfigMap"] == []
    got = registry.reports[0]["projections"]
    assert [p["result"] for p in got] == ["error"] * 4
    assert "gone" in got[0]["message"]
    assert kube.stored() == 3


def test_inventory_that_raises_is_unreadable(kube, registry, env):
    class Boom(type(kube)):
        def get(self, api_version, kind, namespace, name):
            if kind == "Kustomization":
                raise RuntimeError("no such resource")
            return super().get(api_version, kind, namespace, name)

    boom = Boom()
    assert run(boom, registry, env) == 0
    assert [p["result"] for p in registry.reports[0]["projections"]] == ["error"] * 4


# 10. a failed report


def test_failed_report_is_sent_again(kube, registry, env):
    registry.report_error = RegistryError("500", status=500)
    assert run(kube, registry, env) != 0
    assert registry.reports == []
    registry.report_error = None
    assert run(kube, registry, env) == 0
    assert len(registry.reports) == 1
    assert [p["result"] for p in registry.reports[0]["projections"]] == ["unchanged"] * 4


# logging and settings


def test_logs_counts_and_each_changed_object(kube, registry, env, caplog):
    kube.fail_apply = {"db-admin"}
    with caplog.at_level(logging.INFO, logger="registry_provisioner"):
        run(kube, registry, env)
    lines = [r.getMessage() for r in caplog.records]
    assert "pass: applied=3 error=1" in lines
    assert sum("db-admin: error" in line for line in lines) == 1
    caplog.clear()
    kube.fail_apply = set()
    with caplog.at_level(logging.INFO, logger="registry_provisioner"):
        run(kube, registry, env)
        run(kube, registry, env)
    assert [r.getMessage() for r in caplog.records][-1] == "pass: unchanged=4"


def test_missing_settings_exit_2(kube, registry, capsys):
    assert main(["--once"], kube=kube, registry=registry, environ={}) == 2
    assert "PROVISIONER_CLUSTER" in capsys.readouterr().err
