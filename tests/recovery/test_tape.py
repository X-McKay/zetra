import pytest

from zetra._runtime.artifacts import FileStore, IntegrityError, Tape
from zetra._runtime.records import describe


def op(index=0):
    return describe("tool", "inspect", {"arguments": {"path": "."}}, index=index)


def test_candidate_is_not_a_committed_head(tmp_path):
    store = FileStore(tmp_path)
    tape = Tape(store, manifest="probe-v1")
    start = tape.empty()
    receipt = tape.append_candidate(
        start, op(), {"status": "succeeded", "value": {"files": ["README.md"]}}
    )
    assert tape.load(start) == []
    assert receipt.previous == start.head
    assert tape.load(receipt.position)[0]["outcome"] == {"files": ["README.md"]}
    assert receipt.position.cursor == 1


def test_tampered_or_missing_object_stops_recovery(tmp_path):
    store = FileStore(tmp_path)
    tape = Tape(store, manifest="probe-v1")
    receipt = tape.append_candidate(
        tape.empty(), op(), {"status": "succeeded", "value": {"ok": True}}
    )
    (tmp_path / receipt.position.head.sha256).write_bytes(b"corrupt")
    with pytest.raises(IntegrityError):
        tape.load(receipt.position)


def test_unknown_and_wrong_manifest_never_produce_receipt(tmp_path):
    tape = Tape(FileStore(tmp_path), manifest="probe-v1")
    with pytest.raises(ValueError):
        tape.append_candidate(tape.empty(), op(), {"status": "unknown", "value": None})
    operation = op()
    operation["manifest_digest"] = "other"
    with pytest.raises(ValueError):
        tape.append_candidate(tape.empty(), operation, {"status": "succeeded", "value": {}})


def test_large_results_use_refs_and_aggregate_load_is_bounded(tmp_path):
    tape = Tape(FileStore(tmp_path), manifest="probe-v1")
    receipt = tape.append_candidate(
        tape.empty(), op(), {"status": "succeeded", "value": {"text": "x" * 70000}}
    )
    assert receipt.position.head.byte_length < 65536
    assert tape.load(receipt.position)[0]["outcome"]["text"] == "x" * 70000
    limited = Tape(FileStore(tmp_path), manifest="probe-v1", max_bytes=60000)
    with pytest.raises(IntegrityError):
        limited.load(receipt.position)


def test_changed_position_and_out_of_order_append_rejected(tmp_path):
    tape = Tape(FileStore(tmp_path), manifest="probe-v1")
    with pytest.raises(ValueError):
        tape.append_candidate(tape.empty(), op(1), {"status": "succeeded", "value": {}})
    receipt = tape.append_candidate(tape.empty(), op(), {"status": "succeeded", "value": {}})
    wrong = receipt.position.model_copy(update={"prefix_digest": "0" * 64})
    with pytest.raises(IntegrityError):
        tape.load(wrong)
