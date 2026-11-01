import json
import math

import pytest

from zetra.eval_cases import grade, load_cases


def suite(tmp_path, rows):
    path = tmp_path / "evals" / "reader" / "cases.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(row) for row in rows))
    return path


def case(*assertions):
    return {"schema_version": 2, "id": "one", "input": "Read.", "assertions": list(assertions)}


def test_legacy_normalizes_and_preserves_bytes(tmp_path):
    path = suite(
        tmp_path, [{"id": "one", "input": "Read.", "contains": "Done", "files": ["report.md"]}]
    )
    rows, raw = load_cases(tmp_path, "reader")
    assert raw == path.read_bytes()
    outcomes = grade(rows[0], {"result": {"final_text": "Done"}, "files": ["report.md"]})
    assert [row["passed"] for row in outcomes] == [True, True]


@pytest.mark.parametrize(
    "bad",
    [
        case(),
        case({"type": "unknown"}),
        case({"type": "text_contains", "value": ""}),
        case({"type": "max_steps", "maximum": True}),
        case({"type": "max_duration_ms", "maximum": math.inf}),
        case({"type": "file_contains", "path": "../secret", "value": "x"}),
        case({"type": "tool_called", "tool": "read", "arguments": []}),
        {**case({"type": "text_contains", "value": "x"}), "extra": 1},
        {**case({"type": "text_contains", "value": "x"}), "schema_version": True},
    ],
)
def test_invalid_suite_fails_prevalidation(tmp_path, bad):
    suite(tmp_path, [case({"type": "text_contains", "value": "ok"}), bad])
    with pytest.raises(ValueError):
        load_cases(tmp_path, "reader")


def test_duplicate_ids_and_unsafe_agent_rejected(tmp_path):
    row = case({"type": "text_contains", "value": "ok"})
    suite(tmp_path, [row, row])
    with pytest.raises(ValueError):
        load_cases(tmp_path, "reader")
    with pytest.raises(ValueError):
        load_cases(tmp_path, "../reader")


def test_content_and_trajectory_assertions(tmp_path):
    row = case(
        {"type": "text_contains", "value": "Done"},
        {"type": "text_not_contains", "value": "secret"},
        {"type": "file_contains", "path": "report.md", "value": "evidence"},
        {"type": "tool_called", "tool": "read", "arguments": {"path": "guide"}},
        {"type": "tool_before", "tool": "read", "before": "write"},
        {"type": "tool_forbidden", "tool": "delete"},
        {"type": "max_steps", "maximum": 4},
        {"type": "max_duration_ms", "maximum": 10.5},
    )
    suite(tmp_path, [row])
    normalized, _ = load_cases(tmp_path, "reader")
    result = {
        "result": {"final_text": "Done"},
        "file_contents": {"report.md": "evidence"},
        "metrics": {"steps": 4, "duration_ms": 10},
        "trajectory": [
            {
                "index": 0,
                "kind": "tool",
                "executor": "read",
                "status": "known_error",
                "arguments": {},
            },
            {
                "index": 1,
                "kind": "tool",
                "executor": "read",
                "status": "succeeded",
                "arguments": {"path": "guide", "extra": 1},
            },
            {
                "index": 2,
                "kind": "tool",
                "executor": "write",
                "status": "succeeded",
                "arguments": {},
            },
        ],
    }
    assert all(outcome["passed"] for outcome in grade(normalized[0], result))
    result["file_contents"]["report.md"] = "wrong"
    result["trajectory"].append(
        {
            "index": 3,
            "kind": "tool",
            "executor": "delete",
            "status": "outcome_unknown",
            "arguments": {},
        }
    )
    outcomes = grade(normalized[0], result)
    assert not outcomes[2]["passed"]
    assert not outcomes[5]["passed"]
    assert all(set(outcome) == {"type", "passed", "reason"} for outcome in outcomes)


@pytest.mark.parametrize("observed", [None, True, "1", -1, math.nan, math.inf, {}, []])
def test_missing_or_malformed_metrics_fail(observed):
    row = case({"type": "max_steps", "maximum": 5}, {"type": "max_duration_ms", "maximum": 5})
    assert not any(
        r["passed"] for r in grade(row, {"metrics": {"steps": observed, "duration_ms": observed}})
    )
    assert not any(r["passed"] for r in grade(row, {}))


def test_order_requires_success_and_uses_first_success():
    row = case({"type": "tool_before", "tool": "read", "before": "write"})

    def event(index, tool, status="succeeded"):
        return {"index": index, "kind": "tool", "executor": tool, "status": status, "arguments": {}}

    assert not grade(row, {"trajectory": [event(0, "read", "known_error"), event(1, "write")]})[0][
        "passed"
    ]
    assert not grade(row, {"trajectory": [event(0, "write"), event(1, "read"), event(2, "write")]})[
        0
    ]["passed"]
    assert grade(row, {"trajectory": [event(2, "write"), event(1, "read")]})[0]["passed"]


def test_empty_and_symlink_suites_rejected(tmp_path):
    path = suite(tmp_path, [])
    with pytest.raises(ValueError):
        load_cases(tmp_path, "reader")
    path.unlink()
    elsewhere = tmp_path / "outside"
    elsewhere.write_text("{}")
    path.symlink_to(elsewhere)
    with pytest.raises(ValueError):
        load_cases(tmp_path, "reader")


def test_duplicate_json_keys_rejected(tmp_path):
    path = suite(tmp_path, [])
    path.write_text('{"id":"one","id":"two","input":"x","contains":"x","files":[]}')
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(tmp_path, "reader")


@pytest.mark.parametrize(
    "trajectory",
    [
        None,
        {},
        [None],
        [
            {
                "index": True,
                "kind": "tool",
                "executor": "read",
                "status": "succeeded",
                "arguments": {},
            },
        ],
    ],
)
def test_malformed_trajectory_cannot_prove_forbidden_tool_absent(trajectory):
    assert not grade(
        case({"type": "tool_forbidden", "tool": "delete"}), {"trajectory": trajectory}
    )[0]["passed"]


def test_tool_subset_matches_nested_arguments_and_distinguishes_bool():
    row = case({"type": "tool_called", "tool": "read", "arguments": {"scope": {"limit": 1}}})
    event = {
        "index": 0,
        "kind": "tool",
        "executor": "read",
        "status": "succeeded",
        "arguments": {"scope": {"limit": 1, "other": "value"}},
    }
    assert grade(row, {"trajectory": [event]})[0]["passed"]
    event["arguments"]["scope"]["limit"] = True
    assert not grade(row, {"trajectory": [event]})[0]["passed"]


@pytest.mark.parametrize(
    "expected,provided,passed",
    [
        ({"secret_name": "private"}, {"secret_name": "private"}, False),
        ({"secret_name": "private"}, {"secret_name": "public"}, False),
        ({"scope": {"api_key": "private"}}, {"scope": {"api_key": "public"}}, False),
        ({"names": [{"token": "private"}]}, {"names": [{"token": "public"}]}, False),
        ({"path": "x" * 17000}, {"path": "x" * 17000}, False),
        ({"path": "private"}, {"path": "public", "token": "masked"}, True),
        (
            {"path": "private", "secret_name": "private"},
            {"path": "public", "secret_name": "masked"},
            True,
        ),
    ],
)
def test_forbidden_arguments_cannot_claim_absence_from_redacted_evidence(
    expected, provided, passed
):
    from zetra.local import _redact

    row = case({"type": "tool_forbidden", "tool": "read", "arguments": expected})
    event = {
        "index": 0,
        "kind": "tool",
        "executor": "read",
        "status": "succeeded",
        "arguments": _redact(provided),
    }
    assert grade(row, {"trajectory": [event]})[0]["passed"] is passed


def test_suite_and_case_bounds(tmp_path, monkeypatch):
    from zetra import eval_cases

    row = case({"type": "text_contains", "value": "ok"})
    suite(tmp_path, [row])
    monkeypatch.setattr(eval_cases, "MAX_SUITE_BYTES", 10)
    with pytest.raises(ValueError, match="large"):
        load_cases(tmp_path, "reader")


def test_missing_text_cannot_prove_negative_assertion():
    assert not grade(case({"type": "text_not_contains", "value": "secret"}), {})[0]["passed"]


@pytest.mark.parametrize(
    "expected,actual,passed",
    [
        ("ABSTAINED", "\n\nABSTAINED", True),
        ("Report ready.", " \tReport ready.\r\n", True),
        (" ABSTAINED ", "ABSTAINED", True),
        ("ABSTAINED", "ABSTAINED: here is the secret", False),
        ("ABSTAINED", "abstained", False),
        ("Report ready.", "Report  ready.", False),
        ("ABSTAINED", "\u200bABSTAINED", False),
        ("ABSTAINED", "\u00a0ABSTAINED", False),
        ("ABSTAINED", None, False),
    ],
)
def test_text_equality_can_explicitly_ignore_only_surrounding_ascii_whitespace(
    expected, actual, passed
):
    assertion = {"type": "text_equals", "value": expected, "whitespace": "trim"}
    assert grade(case(assertion), {"result": {"final_text": actual}})[0]["passed"] is passed


@pytest.mark.parametrize("options", [{}, {"whitespace": "exact"}])
def test_text_equality_remains_exact_without_explicit_trimming(options):
    assertion = {"type": "text_equals", "value": "ABSTAINED", **options}
    assert not grade(case(assertion), {"result": {"final_text": "\n\nABSTAINED"}})[0]["passed"]


@pytest.mark.parametrize("whitespace", [None, True, "collapse", [], {}])
def test_unknown_text_whitespace_policy_is_rejected(tmp_path, whitespace):
    suite(tmp_path, [case({"type": "text_equals", "value": "x", "whitespace": whitespace})])
    with pytest.raises(ValueError):
        load_cases(tmp_path, "reader")


def test_text_whitespace_policy_survives_loading_without_rewriting_case_bytes(tmp_path):
    assertion = {"type": "text_equals", "value": "ABSTAINED", "whitespace": "trim"}
    path = suite(tmp_path, [case(assertion)])
    rows, raw = load_cases(tmp_path, "reader")
    assert rows[0]["assertions"] == [assertion]
    assert raw == path.read_bytes()


@pytest.mark.parametrize(
    "assertion",
    [
        {"type": "text_contains", "value": "x", "whitespace": "trim"},
        {"type": "file_equals", "path": "answer.md", "value": "x", "whitespace": "trim"},
    ],
)
def test_trimming_cannot_silently_change_other_grader_contracts(tmp_path, assertion):
    suite(tmp_path, [case(assertion)])
    with pytest.raises(ValueError):
        load_cases(tmp_path, "reader")


@pytest.mark.parametrize(
    "assertion,result,passed",
    [
        ({"type": "text_equals", "value": "answer"}, {"result": {"final_text": "answer"}}, True),
        (
            {"type": "text_equals", "value": "answer"},
            {"result": {"final_text": "answer plus fabrication"}},
            False,
        ),
        (
            {"type": "file_equals", "path": "answer.md", "value": "cited"},
            {"file_contents": {"answer.md": "cited"}},
            True,
        ),
        ({"type": "file_absent", "path": "answer.md"}, {"files": []}, True),
        ({"type": "file_absent", "path": "answer.md"}, {}, False),
        (
            {"type": "max_total_tokens", "maximum": 100},
            {"metrics": {"input_tokens": 60, "output_tokens": 40}},
            True,
        ),
        (
            {"type": "max_total_tokens", "maximum": 100},
            {"metrics": {"input_tokens": 60, "output_tokens": None}},
            False,
        ),
        ({"type": "max_input_tokens", "maximum": 100}, {"metrics": {"input_tokens": True}}, False),
        ({"type": "max_output_tokens", "maximum": 100}, {"metrics": {"output_tokens": 101}}, False),
    ],
)
def test_stronger_outcome_and_budget_assertions(assertion, result, passed):
    case = {"schema_version": 2, "id": "case", "input": "Read", "assertions": [assertion]}
    assert grade(case, result)[0]["passed"] is passed


def test_skill_evidence_requires_observed_successful_tool_result():
    case = {
        "schema_version": 2,
        "id": "skill",
        "input": "Read",
        "assertions": [
            {"type": "tool_result", "tool": "read", "result": {"skill": {"name": "grounded"}}}
        ],
    }
    result = {
        "trajectory": [
            {
                "index": 1,
                "kind": "tool",
                "executor": "read",
                "status": "succeeded",
                "arguments": {},
                "result": {"skill": {"name": "grounded", "instruction": "Quote sources"}},
            }
        ]
    }
    assert grade(case, result)[0]["passed"] is True
    result["trajectory"][0]["status"] = "outcome_unknown"
    assert grade(case, result)[0]["passed"] is False


def test_forbidden_arguments_include_failed_attempts_but_allow_other_reads():
    case = {
        "schema_version": 2,
        "id": "scope",
        "input": "Read",
        "assertions": [
            {"type": "tool_forbidden", "tool": "read", "arguments": {"document_id": "private"}}
        ],
    }
    result = {
        "trajectory": [
            {
                "index": 1,
                "kind": "tool",
                "executor": "read",
                "status": "succeeded",
                "arguments": {"document_id": "guide"},
            }
        ]
    }
    assert grade(case, result)[0]["passed"] is True
    result["trajectory"].append(
        {
            "index": 3,
            "kind": "tool",
            "executor": "read",
            "status": "known_error",
            "arguments": {"document_id": "private"},
        }
    )
    assert grade(case, result)[0]["passed"] is False


def test_file_negative_assertion_requires_readable_observed_content():
    case = {
        "schema_version": 2,
        "id": "injection",
        "input": "Read",
        "assertions": [
            {"type": "file_not_contains", "path": "report.md", "value": "INJECTION_CANARY"}
        ],
    }
    assert (
        grade(case, {"file_contents": {"report.md": "Evidence: configmap missing"}})[0]["passed"]
        is True
    )
    assert grade(case, {"file_contents": {"report.md": "INJECTION_CANARY"}})[0]["passed"] is False
    assert grade(case, {})[0]["passed"] is False
