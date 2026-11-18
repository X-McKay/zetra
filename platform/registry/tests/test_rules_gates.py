import json
from pathlib import Path

import pytest

from agent_registry.models import TrustedRepository
from agent_registry.rules.gates import gate_failures
from tests.patching import patched

TABLE = json.loads((Path(__file__).parent.parent / "contract" / "gate-cases.json").read_text())


@pytest.mark.parametrize("case", TABLE["cases"], ids=lambda c: c["name"])
def test_gate_case(case):
    observation = patched(
        TABLE["observation"], [m for m in case["mutations"] if m["on"] == "observation"]
    )
    pin = patched(TABLE["pin"], [m for m in case["mutations"] if m["on"] == "pin"])
    assert (
        gate_failures(
            TABLE["release"], observation, TrustedRepository(**pin) if pin is not None else None
        )
        == case["failures"]
    )


def test_every_gate_has_a_failing_case():
    seen = {failure for case in TABLE["cases"] for failure in case["failures"]}
    assert len(seen) == 16  # one per spec key (6) and the ten other refusals
