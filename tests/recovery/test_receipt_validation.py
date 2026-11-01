import pytest

from zetra._runtime.artifacts import FileStore, Tape
from zetra._runtime.records import describe
from zetra._runtime.workspace import Workspace


@pytest.mark.parametrize("field", [None, "operation_id", "request_digest", "previous"])
def test_receipt_validation_binds_operation_and_accepted_predecessor(tmp_path, field):
    store = FileStore(tmp_path / "objects")
    root = tmp_path / "workspace"
    root.mkdir()
    tape = Tape(store, manifest="probe-v1")
    previous = tape.empty(Workspace(root, store).capture())
    op = describe("tool", "read", {"arguments": {}, "state": {}}, index=0)
    receipt = tape.append_candidate(previous, op, {"status": "succeeded", "value": "read"})
    candidate = receipt.model_dump()
    if field == "previous":
        candidate[field] = receipt.position.head.model_dump()
    elif field is not None:
        candidate[field] = "different"
    if field is None:
        assert tape.validate_receipt(previous, op, candidate) == receipt
    else:
        with pytest.raises(ValueError):
            tape.validate_receipt(previous, op, candidate)
