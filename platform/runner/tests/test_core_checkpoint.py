import copy
import hashlib
import io
import json
import tarfile

import pytest
from agent_runtime.core_protocol import CoreBinding, CoreCommand
from zetra._runtime.artifacts import FileStore, Tape
from zetra._runtime.records import canonical_bytes, describe
from zetra._runtime.workspace import Workspace

from tests.fakes import RELEASE, SESSION, TENANT

TASK = "Write a report."


def binding():
    return CoreBinding(
        profile="zetra-core-v1",
        tenant=TENANT,
        agent="investigator",
        release=RELEASE,
        image="example/investigator@sha256:" + "a" * 64,
        manifest_sha256="b" * 64,
        policy_sha256="c" * 64,
        namespace="default",
        workflow_id=f"session:{TENANT}:investigator:{SESSION}",
        run_id="12345678-1234-4234-8234-123456789abc",
        session_id=SESSION,
        generation=1,
        task_sha256=hashlib.sha256(TASK.encode()).hexdigest(),
    )


def command(position=None, operation=None, action="prepare"):
    return CoreCommand.model_validate(
        {
            "schema_version": 1,
            "binding": binding().model_dump(),
            "task": TASK,
            "action": action,
            "position": position,
            "operation": operation,
        }
    )


def tar_bytes(files, directories=()):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for name in directories:
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
        for name, value in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    return output.getvalue()


@pytest.fixture
def checkpoint(tmp_path):
    root = tmp_path / "sandbox"
    work = root / "workspace"
    work.mkdir(parents=True)
    store = FileStore(root / "state/core/objects")
    workspace = Workspace(work, store)
    tape = Tape(store, manifest=binding().manifest)
    previous = tape.empty(workspace.capture())
    operation = describe(
        "tool",
        "write_report",
        {"arguments": {"text": "hello"}},
        index=0,
        invocation=binding().invocation,
        manifest=binding().manifest,
    )
    (work / "report.txt").write_text("hello")
    receipt = tape.append_candidate(
        previous,
        operation,
        {"status": "succeeded", "value": {"written": True}},
        workspace.capture(),
    )
    files = {
        "workspace/report.txt": b"hello",
        "state/core/lease": b"",
        "state/core/binding.json": canonical_bytes(binding().model_dump(exclude={"generation"})),
        "state/core/attempts/0": canonical_bytes(
            {"position": previous.model_dump(), "operation": operation}
        ),
    }
    files.update({"state/core/objects/" + p.name: p.read_bytes() for p in store.root.iterdir()})
    directories = ["workspace", "state", "state/core", "state/core/objects", "state/core/attempts"]
    return files, directories, command(previous.model_dump(), operation, "execute"), receipt


def test_checkpoint_matches_exact_candidate_and_workspace(checkpoint):
    from runner.core import validate_checkpoint

    files, dirs, request, receipt = checkpoint
    result = validate_checkpoint(tar_bytes(files, dirs), request, receipt.model_dump())
    assert result["position"] == receipt.position.model_dump()
    assert result["boundary"]["kind"] == "tool"
    assert result["boundary"]["boundary"] == 0
    assert result["boundary"]["done"] is False
    assert result["boundary"]["tool"] == "write_report"


@pytest.mark.parametrize(
    "attack",
    [
        "traversal",
        "absolute",
        "token",
        "workspace",
        "missing_object",
        "corrupt_unused",
        "binding",
        "attempt",
        "unexpected_state",
    ],
)
def test_checkpoint_rejects_corruption_and_archive_attacks(checkpoint, attack):
    from runner.core import validate_checkpoint

    files, dirs, request, receipt = checkpoint
    if attack == "traversal":
        files["../escape"] = b"bad"
    elif attack == "absolute":
        files["/workspace/escape"] = b"bad"
    elif attack == "token":
        files["state/token.json"] = b"credential"
    elif attack == "workspace":
        files["workspace/report.txt"] = b"forged"
    elif attack == "missing_object":
        del files["state/core/objects/" + receipt.position.head.sha256]
    elif attack == "corrupt_unused":
        files["state/core/objects/" + "0" * 64] = b"corrupt"
    elif attack == "binding":
        value = json.loads(files["state/core/binding.json"])
        value["run_id"] = "different"
        files["state/core/binding.json"] = canonical_bytes(value)
    elif attack == "attempt":
        files["state/core/attempts/1"] = files["state/core/attempts/0"]
    else:
        files["state/unreviewed.json"] = b"{}"
    with pytest.raises(ValueError):
        validate_checkpoint(tar_bytes(files, dirs), request, receipt.model_dump())


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "duplicate", "oversized"])
def test_tar_metadata_cannot_escape_validation(checkpoint, kind):
    from runner.core import validate_checkpoint

    files, dirs, request, receipt = checkpoint
    data = io.BytesIO(tar_bytes(files, dirs))
    with tarfile.open(fileobj=data, mode="a") as archive:
        member = tarfile.TarInfo("workspace/report.txt" if kind == "duplicate" else "workspace/bad")
        if kind in ("symlink", "hardlink"):
            member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            member.linkname = "/etc/passwd"
        elif kind == "oversized":
            member.size = 1 << 40
        archive.addfile(member)
    with pytest.raises(ValueError):
        validate_checkpoint(data.getvalue(), request, receipt.model_dump())


@pytest.mark.parametrize("field", ["operation_id", "request_digest", "previous"])
def test_receipt_cannot_substitute_metadata(checkpoint, field):
    from runner.core import validate_checkpoint

    files, dirs, request, receipt = checkpoint
    altered = copy.deepcopy(receipt.model_dump())
    altered[field] = receipt.position.head.model_dump() if field == "previous" else "wrong"
    with pytest.raises(ValueError):
        validate_checkpoint(tar_bytes(files, dirs), request, altered)


def test_snapshot_command_holds_the_writer_lease_and_excludes_tokens(tmp_path, checkpoint):
    import fcntl
    import subprocess
    import sys
    from runner.core import CORE_SNAPSHOT, validate_checkpoint

    files, dirs, request, receipt = checkpoint
    root = tmp_path / "live"
    for name in dirs:
        (root / name).mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (root / name).write_bytes(data)
    (root / "state/token.json").write_text("synthetic-secret-cache")
    script = CORE_SNAPSHOT[-1].replace("/sandbox", str(root))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, check=True)
    assert b"synthetic-secret-cache" not in result.stdout
    validate_checkpoint(result.stdout, request, receipt.model_dump())
    with (root / "state/core/lease").open("rb") as lease:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        refused = subprocess.run([sys.executable, "-c", script], capture_output=True, check=False)
        assert refused.returncode != 0 and refused.stdout == b""


def test_restore_rejects_other_execution_and_position(checkpoint):
    from runner.core import validate_restore

    files, dirs, request, receipt = checkpoint
    data = tar_bytes(files, dirs)
    validate_restore(
        data, {**request.binding.model_dump(), "generation": 2}, receipt.position.model_dump()
    )
    for bad in [
        {**request.binding.model_dump(), "task_sha256": "0" * 64},
        {**request.binding.model_dump(), "run_id": "00000000-0000-4000-8000-000000000001"},
    ]:
        with pytest.raises(ValueError):
            validate_restore(data, bad, receipt.position.model_dump())
    with pytest.raises(ValueError):
        validate_restore(data, request.binding, request.position.model_dump())


def restore_command(root, data):
    import sys
    from runner.core import core_restore_command

    cmd = core_restore_command(binding(), hashlib.sha256(data).hexdigest(), len(data))
    return [sys.executable, cmd[1], cmd[2].replace('"/sandbox"', repr(str(root))), *cmd[3:]]


def test_core_restore_refuses_active_execution_lease(checkpoint, tmp_path):
    import fcntl
    import os
    import subprocess

    files, dirs, _, _ = checkpoint
    data = tar_bytes(files, dirs)
    root = tmp_path / "target"
    (root / "state/core").mkdir(parents=True)
    (root / "workspace").mkdir()
    report = root / "workspace/report.txt"
    report.write_text("current tool output")
    with (root / "state/core/lease").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = subprocess.run(
            restore_command(root, data),
            input=data,
            capture_output=True,
            timeout=10,
            env={**os.environ, "AGENT_CORE_BINDING": binding().model_dump_json()},
            check=False,
        )
    assert result.returncode != 0
    assert report.read_text() == "current tool output"


def test_late_restore_after_retry_cannot_overwrite_tool_output(checkpoint, tmp_path):
    import os
    import subprocess

    files, dirs, _, _ = checkpoint
    data = tar_bytes(files, dirs)
    root = tmp_path / "target"
    env = {**os.environ, "AGENT_CORE_BINDING": binding().model_dump_json()}
    cmd = restore_command(root, data)
    # The first exec loses its transport halfway through stdin and arrives late.
    first = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    try:
        first.stdin.write(data[:100])
        first.stdin.flush()
        second = subprocess.run(
            cmd, input=data, capture_output=True, timeout=10, env=env, check=False
        )
        assert second.returncode == 0, second.stderr
        report = root / "workspace/report.txt"
        assert report.read_text() == "hello"
        report.write_text("new tool output")
        first.communicate(data[100:], timeout=10)
        assert first.returncode == 0
        assert report.read_text() == "new tool output"
    finally:
        if first.poll() is None:
            first.kill()
            first.communicate()


def test_incomplete_restore_admission_never_reexecutes(checkpoint, tmp_path):
    import os
    import subprocess

    files, dirs, _, _ = checkpoint
    data = tar_bytes(files, dirs)
    root = tmp_path / "target"
    root.mkdir()
    (root / ".core-restore.json").write_text(json.dumps({"status": "started"}))
    result = subprocess.run(
        restore_command(root, data),
        input=data,
        capture_output=True,
        timeout=10,
        env={**os.environ, "AGENT_CORE_BINDING": binding().model_dump_json()},
        check=False,
    )
    assert result.returncode != 0
    assert not (root / "workspace/report.txt").exists()
