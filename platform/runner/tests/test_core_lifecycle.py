import copy
import hashlib
import json

import pytest
import yaml
from temporalio.exceptions import ApplicationError
from zetra._runtime.records import canonical_bytes

from runner import activities as act, core
from tests.fakes import RELEASE, TENANT
from tests.test_activities import inp, run, setup
from tests.test_core_checkpoint import binding


def world(tmp_path):
    d = setup(tmp_path)
    bound = binding().model_dump()
    release_identity = {
        k: bound[k]
        for k in (
            "profile",
            "tenant",
            "agent",
            "release",
            "image",
            "manifest_sha256",
            "policy_sha256",
        )
    }
    bundle = {
        "version": 1,
        "tenant": TENANT,
        "agent": "investigator",
        "release": RELEASE,
        "image": bound["image"],
        "runtime": {"execution_profile": "zetra-core-v1", "max_boundaries": 64},
    }
    raw = canonical_bytes(bundle)
    sha = hashlib.sha256(raw).hexdigest()
    release_identity["policy_sha256"] = sha
    bound["policy_sha256"] = sha
    release = {
        "spec": {
            "agent": "investigator",
            "image": bound["image"],
            "manifestSha256": bound["manifest_sha256"],
            "policySha256": sha,
            "runtimeProfile": "zetra-core-v1",
            "approval": {"approved": True},
        },
        "status": {
            "ready": True,
            "signatureVerified": True,
            "policyDigest": "sha256:" + sha,
            "identityMode": "agent",
            "policyBundleConfigMap": "policy-" + RELEASE,
        },
    }
    d.custom.get_namespaced_custom_object = lambda *args: release
    pod = yaml.safe_load(d.core.template)
    pod["spec"]["containers"][0]["image"] = bound["image"]
    pod["spec"]["containers"][0]["env"].append(
        {"name": "AGENT_RELEASE_BINDING", "value": json.dumps(release_identity)}
    )
    d.core.template = yaml.safe_dump(pod)
    original = d.core.read_namespaced_config_map
    from kubernetes.client import V1ConfigMap

    def config(name, ns):
        return (
            V1ConfigMap(data={"bundle.json": raw.decode()})
            if name == "policy-" + RELEASE
            else original(name, ns)
        )

    d.core.read_namespaced_config_map = config
    return d, release, bound


def test_preflight_selects_core_only_from_verified_release(tmp_path):
    d, release, bound = world(tmp_path)
    selected = run(act.preflight, inp())
    assert selected == {
        "binding": {
            key: bound[key]
            for key in (
                "profile",
                "tenant",
                "agent",
                "release",
                "image",
                "manifest_sha256",
                "policy_sha256",
            )
        },
        "max_boundaries": 64,
    }
    del release["spec"]["runtimeProfile"]
    assert run(act.preflight, inp()) is None


@pytest.mark.parametrize(
    "mutation", ["unknown_profile", "signature", "digest", "approval", "bundle"]
)
def test_core_preflight_rejects_unverified_profile_or_policy(tmp_path, mutation):
    d, release, _ = world(tmp_path)
    if mutation == "unknown_profile":
        release["spec"]["runtimeProfile"] = "unreviewed"
    elif mutation == "signature":
        release["status"]["signatureVerified"] = False
    elif mutation == "digest":
        release["status"]["policyDigest"] = "sha256:" + "0" * 64
    elif mutation == "approval":
        release["spec"]["approval"]["approved"] = False
    else:
        original = d.core.read_namespaced_config_map

        def bad(name, ns):
            result = original(name, ns)
            if "bundle.json" in result.data:
                result.data["bundle.json"] = "{}"
            return result

        d.core.read_namespaced_config_map = bad
    with pytest.raises(ApplicationError):
        run(act.preflight, inp())
    assert not d.core.created


def test_core_pod_creation_and_adoption_bind_the_exact_run_and_task(tmp_path):
    d, _, bound = world(tmp_path)
    payload = {"binding": bound, "restore": None, "position": None}
    uid = run(core.create_core_sandbox, payload)
    assert uid == run(core.create_core_sandbox, payload)
    assert len(d.core.created) == 1
    pod = next(iter(d.core.pods.values()))
    env = {item.name: item.value for item in pod.spec.containers[0].env}
    assert json.loads(env["AGENT_CORE_BINDING"]) == bound
    changed = copy.deepcopy(bound)
    changed["run_id"] = "00000000-0000-4000-8000-000000000001"
    with pytest.raises(ApplicationError) as error:
        run(core.create_core_sandbox, {**payload, "binding": changed})
    assert error.value.type == "IdentityConflict"


def test_core_template_drift_cannot_create_a_pod(tmp_path):
    d, _, bound = world(tmp_path)
    pod = yaml.safe_load(d.core.template)
    pod["spec"]["containers"][0]["image"] = "wrong-image"
    d.core.template = yaml.safe_dump(pod)
    with pytest.raises(ApplicationError):
        run(core.create_core_sandbox, {"binding": bound, "restore": None, "position": None})
    assert not d.core.created


def test_core_cleanup_cannot_delete_another_execution(tmp_path):
    d, _, bound = world(tmp_path)
    run(core.create_core_sandbox, {"binding": bound, "restore": None, "position": None})
    changed = {**bound, "run_id": "00000000-0000-4000-8000-000000000001"}
    with pytest.raises(ApplicationError):
        run(core.delete_core_sandbox, changed)
    assert not d.core.deleted


def test_core_delete_uses_uid_precondition_and_waits_for_absence(tmp_path):
    d, _, bound = world(tmp_path)
    uid = run(core.create_core_sandbox, {"binding": bound, "restore": None, "position": None})
    original = d.core.delete_namespaced_pod

    def delete(name, namespace, *, body):
        assert body.preconditions.uid == uid
        return original(name, namespace, grace_period_seconds=body.grace_period_seconds)

    d.core.delete_namespaced_pod = delete
    assert run(core.delete_core_sandbox, bound) == "DELETED"
