import pytest

from tests.fixtures.cases import FixtureCase
from tests.fixtures.reference import run_native_reference
from tests.fixtures.remote import fresh_advance
from zetra._runtime.artifacts import FileStore, Tape
from zetra._runtime.dispatch import Dispatcher, OutcomeUnknown
from zetra._runtime.records import describe
from zetra._runtime.workspace import Workspace


async def test_cold_reconstruction_uses_only_explicit_accepted_refs(tmp_path):
    case = FixtureCase()
    reference = await run_native_reference(case)
    store = FileStore(tmp_path / "objects")
    tape = Tape(store, manifest="probe-v1")
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "report").write_text("accepted")
    accepted = tape.empty(Workspace(root, store).capture())
    for index in range(3):
        accepted = tape.append_candidate(
            accepted, case.operations[index], {"status": "succeeded", "value": case.outcomes[index]}
        ).position
    # Upload an extra candidate, then lose all dispatcher/process memory. Neither
    # storage enumeration nor a candidate's existence selects progress.
    tape.append_candidate(
        accepted, case.operations[3], {"status": "succeeded", "value": case.outcomes[3]}
    )
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    new_store = FileStore(tmp_path / "objects")
    Workspace(replacement, new_store).restore(accepted.workspace)
    new_tape = Tape(new_store, manifest="probe-v1")
    result = await fresh_advance(
        {"tape": new_tape.load(accepted), "expected_pending": case.operations[3]}
    )
    assert result["operation"] == case.operations[3]
    assert result["effects"] == []
    assert (replacement / "report").read_text() == "accepted"
    assert reference["final_text"] == "Report ready."


async def test_lost_external_response_blocks_without_redispatch():
    external_effects = []

    async def unsafe(args, state, key):
        external_effects.append(key)
        raise ConnectionError("response lost after effect")

    dispatcher = Dispatcher({"external": unsafe}, manifest="probe-v1")
    operation = describe("tool", "external", {}, index=0)
    for _ in range(2):
        with pytest.raises(OutcomeUnknown):
            await dispatcher.execute(operation, "attempt-1")
    await dispatcher.close()
    assert external_effects == [operation["operation_id"]]
    # Cache loss is explicitly unknown. The recovery owner must not instantiate
    # a new dispatcher and call unsafe merely because the old cache is absent.
