"""Effects a release requests and a tenant grants (skills.md section 7.2)."""

import json
from pathlib import Path

import pytest
from agent_platform import effects

CONTRACT = Path(__file__).parents[2] / "registry" / "contract" / "effects.json"


def namespace(value=None):
    annotations = {} if value is None else {effects.ANNOTATION_GRANTED_EFFECTS: value}
    return {"metadata": {"name": "tenant-test", "annotations": annotations}}


def spec(*names):
    return {"capabilities": {"effects": list(names)}}


def test_the_names_and_statements_are_the_contracts():
    contract = json.loads(CONTRACT.read_text())
    assert effects.ANNOTATION_GRANTED_EFFECTS == contract["annotation"]
    assert {name: e["statement"] for name, e in contract["effects"].items()} == effects.STATEMENTS
    assert effects.statement("sandbox_execute") == (
        "effect sandbox_execute: " + contract["effects"]["sandbox_execute"]["statement"]
    )


@pytest.mark.parametrize(
    "agent_spec",
    [None, {}, {"capabilities": {}}, {"capabilities": {"effects": []}}, {"capabilities": None}],
)
def test_a_spec_that_requests_nothing_needs_no_grant(agent_spec):
    assert effects.requested(agent_spec) == []
    assert effects.ungranted(agent_spec, None) == []


def test_requested_is_sorted_and_without_repeats():
    assert effects.requested(spec("sandbox_execute", "sandbox_execute")) == ["sandbox_execute"]


@pytest.mark.parametrize(
    "ns",
    [None, {}, {"metadata": {}}, {"metadata": {"annotations": None}}, namespace(), namespace("")],
)
def test_without_the_annotation_nothing_is_granted(ns):
    assert effects.granted(ns) == set()
    assert effects.ungranted(spec("sandbox_execute"), ns) == ["sandbox_execute"]


@pytest.mark.parametrize(
    "value", ["sandbox_execute", " sandbox_execute ", "other, sandbox_execute,", "sandbox_execute,"]
)
def test_the_annotation_grants_the_effects_it_names(value):
    assert "sandbox_execute" in effects.granted(namespace(value))
    assert effects.ungranted(spec("sandbox_execute"), namespace(value)) == []


@pytest.mark.parametrize("value", ["sandbox-execute", "SANDBOX_EXECUTE", "sandbox_execute*", "*"])
def test_a_value_that_is_not_the_name_grants_nothing(value):
    assert effects.ungranted(spec("sandbox_execute"), namespace(value)) == ["sandbox_execute"]


def test_an_effect_this_code_does_not_know_is_never_granted():
    ns = namespace("sandbox_execute, remote_write")
    assert effects.ungranted(spec("remote_write", "sandbox_execute"), ns) == ["remote_write"]
