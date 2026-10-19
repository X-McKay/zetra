import pytest
from temporalio.exceptions import ApplicationError

from zetra._runtime.activities import Activities
from zetra._runtime.artifacts import FileStore, Tape
from zetra._runtime.records import describe


def test_receipt_cannot_substitute_an_alternate_accepted_prefix(tmp_path):
    tape = Tape(FileStore(tmp_path), manifest="probe-v1")
    first = describe("tool", "inspect", {}, index=0)
    accepted = tape.append_candidate(
        tape.empty(), first, {"status": "succeeded", "value": "real"}
    ).position
    alternative = tape.append_candidate(
        tape.empty(), first, {"status": "succeeded", "value": "forged"}
    ).position
    second = describe("tool", "write", {}, index=1)
    candidate = tape.append_candidate(
        alternative, second, {"status": "succeeded", "value": "done"}
    ).model_dump()
    candidate["previous"] = accepted.head.model_dump()
    activities = Activities(None, None, tape)

    with pytest.raises(ApplicationError, match="candidate does not extend accepted head"):
        activities.validate_receipt(
            {"position": accepted.model_dump(), "operation": second}, candidate
        )


@pytest.mark.parametrize("corrupt", [False, True])
def test_candidate_requires_verified_workspace_bytes(tmp_path, corrupt):
    from zetra._runtime.artifacts import Ref

    tape = Tape(FileStore(tmp_path), manifest="probe-v1")
    workspace = tape.store.put(b'{"version":1,"files":[],"directories":[]}')
    initial = tape.empty(workspace)
    missing = Ref(sha256="0" * 64, byte_length=1)
    if corrupt:
        missing = tape.store.put(b"not a workspace manifest")
    operation = describe("tool", "inspect", {}, index=0)
    candidate = tape.append_candidate(
        initial, operation, {"status": "succeeded", "value": {}}, missing
    )
    activities = Activities(None, None, tape)

    with pytest.raises((ApplicationError, ValueError)):
        activities.validate_receipt(
            {"position": initial.model_dump(), "operation": operation}, candidate.model_dump()
        )
