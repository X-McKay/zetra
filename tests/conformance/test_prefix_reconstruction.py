import pytest

from tests.fixtures.cases import FixtureCase
from tests.fixtures.reference import run_native_reference
from tests.fixtures.remote import fresh_advance
from zetra._runtime.records import canonical_bytes


@pytest.mark.parametrize("scenario", ["normal", "error", "mutate"])
async def test_every_committed_prefix_reconstructs_without_effects(scenario):
    case = FixtureCase(scenario=scenario)
    reference = await run_native_reference(case)
    tape = [
        {"operation": op, "outcome": result} for op, result in zip(case.operations, case.outcomes)
    ]
    for cursor in range(6):
        observed = await fresh_advance({"scenario": scenario, "tape": tape[:cursor]})
        assert observed["effects"] == []
        assert observed["process_exited"]
        if cursor < 5:
            assert observed["status"] == "operation"
            assert canonical_bytes(observed["operation"]) == canonical_bytes(
                case.operations[cursor]
            )
            # Native trace includes all after callbacks. The replay prefix must
            # agree before capture; cleanup may append after_agent separately.
            assert observed["trace"][:-1] == case.trace[: len(observed["trace"]) - 1]
        else:
            assert observed["status"] == "done"
            assert observed["result"] == reference
            assert observed["trace"] == case.trace


async def test_changed_committed_request_halts():
    case = FixtureCase()
    await run_native_reference(case)
    case.operations[0]["executor"] = "wrong-model"
    observed = await fresh_advance(
        {"tape": [{"operation": case.operations[0], "outcome": case.outcomes[0]}]}
    )
    assert observed["error"] == "ReplayMismatch"
    assert observed["effects"] == []


async def test_changed_pending_descriptor_halts_before_execution():
    observed = await fresh_advance({"tape": [], "expected_pending": {"kind": "tool"}})
    assert observed["error"] == "ReplayMismatch"
    assert observed["effects"] == []
