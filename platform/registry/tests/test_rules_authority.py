import json
from pathlib import Path

import pytest
import yaml

from agent_registry.canonical import authority_of
from agent_registry.rules.authority import widens
from tests.patching import patched

HERE = Path(__file__).parent
TABLE = json.loads((HERE.parent / "contract" / "authority-cases.json").read_text())


@pytest.mark.parametrize("case", TABLE["cases"], ids=lambda c: c["name"])
def test_authority_case(case):
    assert widens(TABLE["base"], patched(TABLE["base"], case["mutations"])) == case["reasons"]


def test_nothing_approved_before_is_always_more():
    assert widens({}, TABLE["base"]) == TABLE["emptyBase"]
    assert widens({}, {}) == TABLE["emptyBase"]


def test_the_base_is_the_fixture_agent():
    agent = yaml.safe_load(
        (HERE / "fixtures" / "manifests" / "tenant-test" / "db-admin" / "agent.yaml").read_text()
    )
    assert authority_of(agent) == TABLE["base"]
