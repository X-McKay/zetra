"""What the registry checks in a reported release besides the lifecycle's own rules
(`contract/delivery.md` sections 1.3 and 2). No HTTP here: the routes that take a report call
these and let the `Refusal` leave.
"""

from __future__ import annotations

import re
from typing import Any

from agent_registry.rules import Refusal

CASE_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*\Z")
MAX_CASES, MAX_TASK, MAX_LIST, MAX_ENTRY = 20, 4000, 20, 500
CHECKS = ("report_contains", "report_matches", "report_excludes")


def _invalid(message: str) -> Refusal:
    return Refusal("invalid-arguments", message)


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _only(where: str, document: dict[str, Any], allowed: tuple[str, ...]) -> None:
    for key in document:
        if key not in allowed:
            raise _invalid(f"{where}: unknown key {key!r}")


def check_suite(suite: Any) -> None:
    """Refuse a suite that breaks a rule of section 1.3; the message names the key at fault."""
    if not isinstance(suite, dict):
        raise _invalid("evaluation.suite: must be an object")
    where = "evaluation.suite"
    _only(where, suite, ("schema_version", "timeout_seconds", "acceptance", "cases"))
    if not _whole(suite.get("schema_version")) or suite["schema_version"] != 1:
        raise _invalid(f"{where}.schema_version: must be 1")
    timeout = suite.get("timeout_seconds")
    if not _whole(timeout) or not 30 <= timeout <= 3600:
        raise _invalid(f"{where}.timeout_seconds: must be a whole number from 30 to 3600")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise _invalid(f"{where}.cases: must be a list of 1 to {MAX_CASES} cases")
    acceptance = suite.get("acceptance")
    if not isinstance(acceptance, dict):
        raise _invalid(f"{where}.acceptance: must be an object")
    _only(f"{where}.acceptance", acceptance, ("min_passed",))
    wanted = acceptance.get("min_passed")
    if wanted != "all" and (not _whole(wanted) or not 1 <= wanted <= len(cases)):
        raise _invalid(
            f'{where}.acceptance.min_passed: must be "all" or a whole number from 1 to {len(cases)}'
        )
    seen: set[str] = set()
    for index, case in enumerate(cases):
        _check_case(f"{where}.cases[{index}]", case, seen)


def _check_case(where: str, case: Any, seen: set[str]) -> None:
    if not isinstance(case, dict):
        raise _invalid(f"{where}: must be an object")
    _only(where, case, ("id", "task", "expect"))
    name = case.get("id")
    if not isinstance(name, str) or not CASE_ID.match(name):
        raise _invalid(f"{where}.id: must be lowercase letters, digits and hyphens")
    if name in seen:
        raise _invalid(f"{where}.id: {name!r} is used twice")
    seen.add(name)
    task = case.get("task")
    if not isinstance(task, str) or not 1 <= len(task) <= MAX_TASK:
        raise _invalid(f"{where}.task: must be a string of 1 to {MAX_TASK} characters")
    expect = case.get("expect", {})
    if not isinstance(expect, dict):
        raise _invalid(f"{where}.expect: must be an object")
    _only(f"{where}.expect", expect, ("outcome", *CHECKS))
    if expect.get("outcome", "completed") != "completed":
        raise _invalid(f'{where}.expect.outcome: the only value is "completed"')
    for key in CHECKS:
        entries = expect.get(key, [])
        here = f"{where}.expect.{key}"
        if not isinstance(entries, list) or len(entries) > MAX_LIST:
            raise _invalid(f"{here}: must be a list of at most {MAX_LIST} strings")
        for entry in entries:
            if not isinstance(entry, str) or len(entry) > MAX_ENTRY:
                raise _invalid(f"{here}: each entry is a string of at most {MAX_ENTRY} characters")
            if key == "report_matches":
                try:
                    re.compile(entry)
                except re.error as error:
                    raise _invalid(f"{here}: {entry!r} does not compile ({error})") from None


def check_declaration(agent: dict[str, Any], agent_release: dict[str, Any]) -> None:
    """A release that carries its declaration must carry the Agent's spec, without `enabled`
    (section 2). A release without one is accepted."""
    spec = agent_release.get("spec")
    if not isinstance(spec, dict) or "declaration" not in spec:
        return
    agent_spec = agent.get("spec")
    expected = (
        {k: v for k, v in agent_spec.items() if k != "enabled"}
        if isinstance(agent_spec, dict)
        else None
    )
    if spec["declaration"] != expected:
        raise _invalid("agentRelease.spec.declaration: is not the Agent's spec without `enabled`")
