import copy
import hashlib
import json

import yaml

import pytest
from agent_runtime.core_protocol import encode_frame
from temporalio.exceptions import ApplicationError
from zetra._runtime.records import describe

from runner import activities as act, core
from tests.test_activities import run, setup
from tests import test_core_checkpoint as checkpoints
from tests.test_core_checkpoint import binding, command, tar_bytes
from tests.fakes import model

checkpoint = checkpoints.checkpoint


def reply(code, data, **kwargs):
    return act.ExecResult(code, encode_frame(data), "", **{"truncated": False, **kwargs})


def install_pod(deps):
    bound = binding()
    pod = yaml.safe_load(deps.core.template)
    pod["metadata"].update(name=act.pod_name(bound.session_id, bound.generation), uid="owned-pod")
    pod["metadata"]["labels"].update(
        {act.LABEL + "session": bound.session_id, act.LABEL + "generation": str(bound.generation)}
    )
    container = pod["spec"]["containers"][0]
    container["image"] = bound.image
    container["env"].append({"name": "AGENT_CORE_BINDING", "value": json.dumps(bound.model_dump())})
    deps.core.pods[pod["metadata"]["name"]] = model(pod, "V1Pod")


def transport(tmp_path, response):
    deps = setup(tmp_path)
    install_pod(deps)
    calls = []

    def execute(*args, **kwargs):
        calls.append((args, kwargs))
        if isinstance(response, Exception):
            raise response
        return response

    deps.exec = execute
    return deps, calls


def test_prepare_returns_only_the_requested_accepted_position(tmp_path, checkpoint):
    _, _, request, _ = checkpoint
    prepared = {
        "status": "operation",
        "position": request.position.model_dump(),
        "operation": request.operation,
    }
    _, calls = transport(tmp_path, reply(0, prepared))
    result = run(core.prepare_core, command(request.position.model_dump()).model_dump())
    assert result == prepared and len(calls) == 1


def test_prepare_loss_is_distinct_from_an_execute_loss(tmp_path):
    transport(tmp_path, act.ExecLost("synthetic"))
    with pytest.raises(ApplicationError) as failed:
        run(core.prepare_core, command().model_dump())
    assert failed.value.type == "CorePrepareLost"


@pytest.mark.parametrize(
    "failure",
    ["lost", "not_started", "malformed", "timeout", "truncated", "invalid", "wrong_receipt"],
)
def test_execute_uncertainty_never_authorizes_retry(tmp_path, checkpoint, failure):
    _, _, request, receipt = checkpoint
    result = {"status": "candidate", "receipt": receipt.model_dump()}
    response = reply(0, result)
    if failure == "lost":
        response = act.ExecLost("synthetic")
    elif failure == "not_started":
        response = act.ExecNotStarted("synthetic")
    elif failure == "malformed":
        response.stdout = b"not JSON"
    elif failure == "timeout":
        response.timed_out = True
    elif failure == "truncated":
        response.truncated = True
    elif failure == "invalid":
        response = reply(5, {"status": "invalid"})
    else:
        result["receipt"]["request_digest"] = "wrong"
        response = reply(0, result)
    _, calls = transport(tmp_path, response)
    assert run(core.execute_core, request.model_dump()) == {"status": "unknown"}
    assert len(calls) == 1


@pytest.mark.parametrize("kind", ["model", "tool"])
@pytest.mark.parametrize("code,reason", [(3, "policy"), (4, "budget")])
def test_refusal_is_definitive_only_for_a_model(tmp_path, checkpoint, kind, code, reason):
    _, _, request, _ = checkpoint
    operation = describe(
        kind, "executor", {}, index=0, invocation=binding().invocation, manifest=binding().manifest
    )
    request = command(request.position.model_dump(), operation, "execute")
    transport(tmp_path, reply(code, {"status": "refused", "reason": reason}))
    assert run(core.execute_core, request.model_dump()) == {
        "status": "refused" if kind == "model" else "unknown",
        "reason": reason,
    }


def test_candidate_is_not_accepted_until_snapshot_validation_and_storage(tmp_path, checkpoint):
    files, dirs, request, receipt = checkpoint
    payload = {"status": "candidate", "receipt": receipt.model_dump()}
    deps, _ = transport(tmp_path, reply(0, payload))
    assert run(core.execute_core, request.model_dump()) == payload
    assert not list((tmp_path / "artifacts").rglob("*"))
    data = tar_bytes(files, dirs)

    def snapshot(*args, **kwargs):
        kwargs["on_stdout"](data)
        return act.ExecResult(0, b"", "", False)

    deps.exec = snapshot
    result = run(
        core.snapshot_core, {"command": request.model_dump(), "receipt": receipt.model_dump()}
    )
    assert result["position"] == receipt.position.model_dump()
    assert result["snapshot"] == hashlib.sha256(data).hexdigest()
    with deps.store.open(result["snapshot"]) as stored:
        assert stored.read() == data


def test_corrupt_existing_storage_cannot_be_reported_durable(tmp_path, checkpoint):
    files, dirs, request, receipt = checkpoint
    data = tar_bytes(files, dirs)
    deps = setup(tmp_path)
    install_pod(deps)

    def snapshot(*args, **kwargs):
        kwargs["on_stdout"](data)
        return act.ExecResult(0, b"", "", False)

    deps.exec = snapshot
    dest = deps.store._path(hashlib.sha256(data).hexdigest())
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"corrupt")
    with pytest.raises(ApplicationError):
        run(core.snapshot_core, {"command": request.model_dump(), "receipt": receipt.model_dump()})


def test_prepare_cannot_choose_a_newer_position(tmp_path, checkpoint):
    _, _, request, receipt = checkpoint
    prepared = {
        "status": "operation",
        "position": receipt.position.model_dump(),
        "operation": copy.deepcopy(request.operation),
    }
    transport(tmp_path, reply(0, prepared))
    with pytest.raises(ApplicationError):
        run(core.prepare_core, command(request.position.model_dump()).model_dump())


def test_wrong_pod_binding_cannot_prepare_execute_or_snapshot(tmp_path, checkpoint):
    _, _, request, receipt = checkpoint
    deps, calls = transport(
        tmp_path, reply(0, {"status": "candidate", "receipt": receipt.model_dump()})
    )
    pod = next(iter(deps.core.pods.values()))
    next(
        item for item in pod.spec.containers[0].env if item.name == "AGENT_CORE_BINDING"
    ).value = "{}"
    with pytest.raises(ApplicationError):
        run(core.prepare_core, command(request.position.model_dump()).model_dump())
    assert run(core.execute_core, request.model_dump()) == {"status": "unknown"}
    with pytest.raises(ApplicationError):
        run(core.snapshot_core, {"command": request.model_dump(), "receipt": receipt.model_dump()})
    assert not calls


def test_concurrent_snapshots_do_not_buffer_multiple_archives(tmp_path, checkpoint):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    files, dirs, request, receipt = checkpoint
    data = tar_bytes(files, dirs)
    deps = setup(tmp_path)
    install_pod(deps)
    first_started, second_started, release_first = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    calls = []

    def snapshot(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(5)
        else:
            second_started.set()
        kwargs["on_stdout"](data)
        return act.ExecResult(0, b"", "", False)

    deps.exec = snapshot
    payload = {"command": request.model_dump(), "receipt": receipt.model_dump()}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, core.snapshot_core, payload)
        assert first_started.wait(5)
        second = pool.submit(run, core.snapshot_core, payload)
        try:
            assert not second_started.wait(0.2)
        finally:
            release_first.set()
        assert first.result()["position"] == second.result()["position"]


@pytest.mark.parametrize("timed_out", [False, True])
def test_ready_restores_through_fenced_core_command(tmp_path, checkpoint, timed_out):
    import io
    from kubernetes.client import V1PodStatus

    files, dirs, _, receipt = checkpoint
    data = tar_bytes(files, dirs)
    key = hashlib.sha256(data).hexdigest()
    deps, calls = transport(tmp_path, act.ExecResult(0, b"", "", False, timed_out=timed_out))
    pod = next(iter(deps.core.pods.values()))
    pod.status = V1PodStatus(phase="Running")
    deps.store.put(key, io.BytesIO(data))
    payload = {
        "binding": binding().model_dump(),
        "restore": key,
        "position": receipt.position.model_dump(),
    }
    if timed_out:
        with pytest.raises(ApplicationError, match="restore not confirmed"):
            run(core.wait_core_ready, payload)
    else:
        run(core.wait_core_ready, payload)
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[2] == core.core_restore_command(binding(), key, len(data))
    assert callable(kwargs["heartbeat"])
