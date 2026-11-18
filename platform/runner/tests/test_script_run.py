"""Skills contract section 10: the script_run key of a skill_run boundary."""

from __future__ import annotations

import copy
from dataclasses import asdict

import pytest

from runner.types import SCRIPT_RUN_KEYS, BoundaryOut, Usage
from tests.harness import out

DIGEST = "sha256:" + "ab" * 32


def run_doc(**over) -> dict:
    doc = {
        "skill": "row-count",
        "script": "scripts/count.py",
        "digest": DIGEST,
        "argument_count": 2,
        "arguments_sha256": "5d" * 32,
        "exit_status": 0,
        "limits_hit": [],
        "refused": None,
        "duration_ms": 41,
    }
    doc.update(over)
    return doc


def boundary_doc(tool="skill_run", **over) -> dict:
    doc = {
        "boundary": 3,
        "kind": "tool",
        "done": False,
        "tool": tool,
        "usage": None,
        "summary": "ok",
    }
    if tool == "skill_run":
        doc["script_run"] = run_doc()
    doc.update(over)
    return doc


def test_valid_script_run_parses():
    parsed = BoundaryOut.parse_strict(boundary_doc())
    assert parsed.script_run == run_doc()
    assert parsed.tool == "skill_run"


def test_refused_run_is_accepted():
    refused = run_doc(
        skill=None, script=None, digest=None, exit_status=None, refused="unknown_skill"
    )
    assert BoundaryOut.parse_strict(boundary_doc(script_run=refused)).script_run == refused
    partial = run_doc(script=None, exit_status=None, refused="unknown_script")
    assert BoundaryOut.parse_strict(boundary_doc(script_run=partial)).script_run == partial


def test_limited_run_is_accepted():
    hit = run_doc(exit_status=None, limits_hit=["memory", "time"])
    assert BoundaryOut.parse_strict(boundary_doc(script_run=hit)).script_run == hit


BAD = [
    ("skill", "Row-Count"),
    ("skill", "row--count"),
    ("skill", "a" * 65),
    ("skill", 3),
    ("script", "count.py"),
    ("script", "scripts/../x.py"),
    ("script", "scripts/a b.py"),
    ("script", "/scripts/a.py"),
    ("script", "scripts/" + "a" * 130),
    ("script", 3),
    ("digest", "sha256:" + "AB" * 32),
    ("digest", "sha256:" + "ab" * 31),
    ("digest", "ab" * 32),
    ("argument_count", 33),
    ("argument_count", -1),
    ("argument_count", True),
    ("argument_count", 1.0),
    ("arguments_sha256", "5D" * 32),
    ("arguments_sha256", "5d" * 31),
    ("arguments_sha256", None),
    ("exit_status", 256),
    ("exit_status", -65),
    ("exit_status", False),
    ("exit_status", "0"),
    ("limits_hit", ["time", "memory"]),
    ("limits_hit", ["time", "time"]),
    ("limits_hit", ["cpu"]),
    ("limits_hit", "time"),
    ("limits_hit", None),
    ("refused", "nope"),
    ("refused", 1),
    ("duration_ms", -1),
    ("duration_ms", True),
    ("duration_ms", None),
]


@pytest.mark.parametrize(("key", "value"), BAD)
def test_bad_value_is_rejected(key, value):
    with pytest.raises(ValueError):
        BoundaryOut.parse_strict(boundary_doc(script_run=run_doc(**{key: value})))


@pytest.mark.parametrize("key", sorted(SCRIPT_RUN_KEYS))
def test_missing_key_is_rejected(key):
    run = run_doc()
    del run[key]
    with pytest.raises(ValueError):
        BoundaryOut.parse_strict(boundary_doc(script_run=run))


def test_extra_key_is_rejected():
    with pytest.raises(ValueError):
        BoundaryOut.parse_strict(boundary_doc(script_run=run_doc(stdout="x")))


@pytest.mark.parametrize("run", [None, [], "x"])
def test_script_run_must_be_an_object_on_skill_run(run):
    with pytest.raises(ValueError):
        BoundaryOut.parse_strict(boundary_doc(script_run=run))


@pytest.mark.parametrize("tool", ["pods_list", None])
def test_script_run_on_another_tool_is_rejected(tool):
    doc = boundary_doc(tool=tool, script_run=run_doc())
    with pytest.raises(ValueError):
        BoundaryOut.parse_strict(doc)


def test_skill_run_without_script_run_is_rejected():
    doc = boundary_doc()
    del doc["script_run"]
    with pytest.raises(ValueError):
        BoundaryOut.parse_strict(doc)


def test_existing_boundary_output_is_unaffected():
    doc = boundary_doc(tool="pods_get", usage={"input_tokens": 1, "output_tokens": 2})
    parsed = BoundaryOut.parse_strict(doc)
    assert parsed.script_run is None
    assert parsed.usage == Usage(1, 2)
    assert BoundaryOut.parse_strict(asdict(out(1))) == out(1)


def test_script_run_survives_asdict():
    parsed = BoundaryOut.parse_strict(boundary_doc())
    assert BoundaryOut.parse_strict(copy.deepcopy(asdict(parsed))) == parsed
