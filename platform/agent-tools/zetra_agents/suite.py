"""The evaluation suite an agent may ship in `agents/<name>/evaluation/suite.yaml`.

The rules are those of delivery.md section 1.3, and the registry applies the same ones when
a release is reported; `registry/contract/suite-cases.json` holds suites both must accept and
suites both must refuse. `problems` takes the parsed document so a caller can hold any suite
to the rules without a file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

CASE_ID = re.compile(r"^[a-z0-9-]+$")
TOP_KEYS = ("schema_version", "timeout_seconds", "acceptance", "cases")
CASE_KEYS = ("id", "task", "expect")
EXPECT_KEYS = ("outcome", "report_contains", "report_matches", "report_excludes")
MAX_CASES = 20
MAX_TASK = 4000
MAX_ENTRIES = 20
MAX_ENTRY = 500
TIMEOUT = (30, 3600)


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _unknown(where: str, document: dict, allowed: tuple[str, ...]) -> list[str]:
    prefix = f"{where}." if where else ""
    return [
        f"{prefix}{key}: unknown key; allowed: {', '.join(allowed)}"
        for key in document
        if key not in allowed
    ]


def _checks(where: str, key: str, value: Any) -> list[str]:
    path = f"{where}.{key}"
    if not isinstance(value, list):
        return [f"{path}: must be a list of strings"]
    found = []
    if len(value) > MAX_ENTRIES:
        found.append(f"{path}: at most {MAX_ENTRIES} entries")
    for index, entry in enumerate(value):
        if not isinstance(entry, str):
            found.append(f"{path}[{index}]: must be a string")
        elif len(entry) > MAX_ENTRY:
            found.append(f"{path}[{index}]: at most {MAX_ENTRY} characters")
        elif key == "report_matches":
            try:
                re.compile(entry)
            except (re.error, RecursionError) as exc:
                found.append(f"{path}[{index}]: pattern does not compile: {exc}")
    return found


def _case(index: int, case: Any, seen: set[str]) -> list[str]:
    where = f"cases[{index}]"
    if not isinstance(case, dict):
        return [f"{where}: must be a mapping"]
    found = _unknown(where, case, CASE_KEYS)
    case_id = case.get("id")
    if not isinstance(case_id, str) or not CASE_ID.match(case_id):
        found.append(f"{where}.id: must be a name of lowercase letters, digits and hyphens")
    elif case_id in seen:
        found.append(f"{where}.id: {case_id!r} is used by an earlier case; ids are unique")
    else:
        seen.add(case_id)
    task = case.get("task")
    if not isinstance(task, str) or not 1 <= len(task) <= MAX_TASK:
        found.append(f"{where}.task: must be a string of 1 to {MAX_TASK} characters")
    expect = case.get("expect", {})
    if expect is None:
        expect = {}
    if not isinstance(expect, dict):
        return found + [f"{where}.expect: must be a mapping"]
    found += _unknown(f"{where}.expect", expect, EXPECT_KEYS)
    if expect.get("outcome", "completed") != "completed":
        found.append(f"{where}.expect.outcome: the only value is 'completed'")
    for key in EXPECT_KEYS[1:]:
        if key in expect:
            found += _checks(f"{where}.expect", key, expect[key])
    return found


def problems(document: Any) -> list[str]:
    """Every rule of section 1.3 the suite breaks, each naming the key at fault."""
    if not isinstance(document, dict):
        return ["the suite must be a mapping"]
    found = _unknown("", document, TOP_KEYS)
    if document.get("schema_version") != 1 or not _whole(document.get("schema_version")):
        found.append("schema_version: must be 1")
    timeout = document.get("timeout_seconds")
    if not _whole(timeout) or not TIMEOUT[0] <= timeout <= TIMEOUT[1]:
        found.append(f"timeout_seconds: required, a whole number from {TIMEOUT[0]} to {TIMEOUT[1]}")
    cases = document.get("cases")
    count = len(cases) if isinstance(cases, list) else 0
    if not isinstance(cases, list) or not 1 <= count <= MAX_CASES:
        found.append(f"cases: required, a list of 1 to {MAX_CASES} cases")
    else:
        seen: set[str] = set()
        for index, case in enumerate(cases):
            found += _case(index, case, seen)
    acceptance = document.get("acceptance")
    if not isinstance(acceptance, dict):
        found.append("acceptance: required, a mapping with min_passed")
    else:
        found += _unknown("acceptance", acceptance, ("min_passed",))
        wanted = acceptance.get("min_passed")
        if wanted != "all" and not (_whole(wanted) and 1 <= wanted <= max(count, 1)):
            found.append(
                f"acceptance.min_passed: must be 'all' or a whole number from 1 to the "
                f"number of cases ({count})"
            )
    return found


def file_problems(path: Path, name: str) -> list[str]:
    """`problems` for the suite file at `path`, or nothing when there is no such file."""
    if not path.is_file():
        return []
    label = f"{name}: evaluation/suite.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return [f"{label} is not UTF-8 text"]
    except OSError as exc:
        return [f"{label} cannot be read: {exc.strerror or type(exc).__name__}"]
    except RecursionError:
        return [f"{label} is nested too deeply to be read"]
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        return [f"{label} is malformed YAML{where}"]
    return [f"{label}: {p}" for p in problems(document)]
