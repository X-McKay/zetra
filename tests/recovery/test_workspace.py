import json

import pytest

from zetra._runtime.artifacts import FileStore, Tape
from zetra._runtime.dispatch import Dispatcher, KnownError, OutcomeUnknown
from zetra._runtime.records import describe
from zetra._runtime.workspace import Workspace, WorkspaceError


def test_roundtrip_and_reject_overlay(tmp_path):
    store = FileStore(tmp_path / "objects")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "nested" / "report").write_bytes(b"report\x00")
    ws = Workspace(root, store)
    ref = ws.capture()
    target = tmp_path / "replacement"
    target.mkdir()
    Workspace(target, store).restore(ref)
    assert (target / "nested" / "report").read_bytes() == b"report\x00"
    with pytest.raises(WorkspaceError):
        Workspace(target, store).restore(ref)


@pytest.mark.parametrize("name", ["../escape", "/tmp/escape", "a/../../escape", "a//b", "."])
def test_unsafe_manifest_paths(tmp_path, name):
    store = FileStore(tmp_path / "objects")
    root = tmp_path / "workspace"
    root.mkdir()
    ref = store.put(
        json.dumps(
            {"version": 1, "files": [{"path": name, "data": "eA=="}], "directories": []}
        ).encode()
    )
    with pytest.raises(WorkspaceError):
        Workspace(root, store).restore(ref)
    assert not list(root.iterdir())


def test_links_budget_and_writers(tmp_path):
    store = FileStore(tmp_path / "objects")
    root = tmp_path / "workspace"
    root.mkdir()
    ws = Workspace(root, store, max_bytes=4)
    (root / "link").symlink_to("/etc/passwd")
    with pytest.raises(WorkspaceError):
        ws.capture()
    (root / "link").unlink()
    (root / "big").write_bytes(b"12345")
    with pytest.raises(WorkspaceError):
        ws.capture()
    (root / "big").unlink()
    with ws.writer(), pytest.raises(WorkspaceError):
        ws.capture()


@pytest.mark.parametrize("business_error", [False, True])
async def test_outcome_checkpoint_is_indivisible(tmp_path, business_error):
    store = FileStore(tmp_path / "objects")
    root = tmp_path / "workspace"
    root.mkdir()
    ws = Workspace(root, store)
    tape = Tape(store, manifest="probe-v1")
    initial = tape.empty(ws.capture())

    async def body(args, state, key):
        (root / "report").write_text("retained")
        if business_error:
            raise KnownError({"error": "report incomplete"})
        return {"path": "report"}

    dispatcher = Dispatcher({"write": body}, manifest="probe-v1", workspace=ws)
    operation = describe("tool", "write", {"arguments": {}}, index=0)
    outcome = await dispatcher.execute(operation, "attempt-1")
    receipt = tape.append_candidate(initial, operation, outcome, dispatcher.checkpoint(operation))
    target = tmp_path / "replacement"
    target.mkdir()
    Workspace(target, store).restore(receipt.position.workspace)
    assert (target / "report").read_text() == "retained"
    assert outcome["status"] == ("known_error" if business_error else "succeeded")
    await dispatcher.close()


async def test_failed_checkpoint_never_returns_success(tmp_path, monkeypatch):
    store = FileStore(tmp_path / "objects")
    root = tmp_path / "ws"
    root.mkdir()
    ws = Workspace(root, store)

    async def body(args, state, key):
        (root / "report").write_text("uncommitted")
        return {}

    def fail():
        raise OSError("upload lost")

    monkeypatch.setattr(ws, "capture", fail)
    dispatcher = Dispatcher({"write": body}, manifest="probe-v1", workspace=ws)
    with pytest.raises(OutcomeUnknown):
        await dispatcher.execute(describe("tool", "write", {}, index=0), "a")
    await dispatcher.close()


@pytest.mark.parametrize("name", ["x" * 256, "/".join(["x"] * 33), "/".join(["x" * 100] * 11)])
def test_unrestorable_path_limits_rejected(tmp_path, name):
    store = FileStore(tmp_path / "objects")
    root = tmp_path / "ws"
    root.mkdir()
    ref = store.put(
        json.dumps(
            {"version": 1, "files": [{"path": name, "data": "eA=="}], "directories": []}
        ).encode()
    )
    with pytest.raises(WorkspaceError):
        Workspace(root, store).restore(ref)
    assert not list(root.iterdir())


def test_implicit_parents_count_toward_entry_budget(tmp_path):
    store = FileStore(tmp_path / "objects")
    root = tmp_path / "ws"
    root.mkdir()
    ref = store.put(
        json.dumps(
            {"version": 1, "files": [{"path": "a/b/c", "data": "eA=="}], "directories": []}
        ).encode()
    )
    with pytest.raises(WorkspaceError):
        Workspace(root, store, max_files=2).restore(ref)


def test_capture_ignores_access_time_changed_by_its_own_read(tmp_path):
    import os

    store = FileStore(tmp_path / "objects")
    root = tmp_path / "ws"
    root.mkdir()
    path = root / "report"
    path.write_text("unchanged")
    os.utime(path, (1, path.stat().st_mtime))
    assert Workspace(root, store).capture()
