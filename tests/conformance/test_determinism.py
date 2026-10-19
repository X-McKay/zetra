import random
import uuid

from google.adk.platform.time import get_time
from google.adk.platform.uuid import new_uuid

from tests.fixtures.cases import FixtureCase
from tests.fixtures.reference import run_native_reference
from tests.fixtures.remote import fresh_advance
from zetra._runtime.adk_compat import deterministic_runtime
from zetra._runtime.records import canonical_bytes


def test_external_entropy_does_not_change_reconstruction_stream():
    with deterministic_runtime():
        first = new_uuid()
        random.random()
        uuid.uuid4()
        second = new_uuid()
        assert get_time() == 1700000000.0
    with deterministic_runtime():
        assert [new_uuid(), new_uuid()] == [first, second]


async def test_twenty_fresh_launches_reconstruct_all_prefixes():
    case = FixtureCase()
    reference = await run_native_reference(case)
    tape = [{"operation": o, "outcome": v} for o, v in zip(case.operations, case.outcomes)]
    for _ in range(20):
        for cursor in range(6):
            result = await fresh_advance({"tape": tape[:cursor]})
            assert result["effects"] == []
            if cursor < 5:
                assert canonical_bytes(result["operation"]) == canonical_bytes(
                    case.operations[cursor]
                )
            else:
                assert result["result"] == reference
