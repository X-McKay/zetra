import json

import pytest

from zetra import evaluation
from zetra.scaffold import scaffold


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    scaffold(root, "evaluated_reader")
    return root


def cases(root, values):
    (root / "evals/document_reader/cases.jsonl").write_text(
        "".join(json.dumps(value) + "\n" for value in values)
    )


async def test_every_case_is_accounted_for_when_execution_fails(project, monkeypatch):
    cases(
        project,
        [
            {"id": name, "input": name, "contains": "answer", "files": []}
            for name in ("good", "wrong", "error")
        ],
    )

    async def run(root, message, *, agent=None):
        if message == "error":
            raise RuntimeError("sensitive provider error")
        return {
            "status": "SUCCEEDED",
            "result": {"final_text": "answer" if message == "good" else "incorrect"},
            "files": [],
        }

    monkeypatch.setattr(evaluation, "fixture_run", run)
    report = await evaluation.evaluate(project, mode="fixture")
    assert report["status"] == "FAILED"
    assert report["counts"] == {"planned": 3, "passed": 1, "failed": 1, "error": 1}
    assert [row["outcome"] for row in report["cases"]] == ["passed", "failed", "error"]
    assert "sensitive provider error" not in json.dumps(report)


async def test_invalid_later_case_prevents_all_execution(project, monkeypatch):
    marker = project / "executed"
    cases(
        project,
        [{"id": "good", "input": "read", "contains": "answer", "files": []}, {"id": "invalid"}],
    )

    async def run(*args, **kwargs):
        marker.write_text("executed")
        return {"status": "SUCCEEDED"}

    monkeypatch.setattr(evaluation, "fixture_run", run)
    with pytest.raises(ValueError):
        await evaluation.evaluate(project, mode="fixture")
    assert not marker.exists()


async def test_plausible_answer_cannot_pass_without_required_tool_evidence(project, monkeypatch):
    cases(
        project,
        [
            {
                "schema_version": 2,
                "id": "read",
                "input": "Read guide",
                "assertions": [
                    {"type": "text_contains", "value": "answer"},
                    {
                        "type": "tool_called",
                        "tool": "read_document",
                        "arguments": {"document_id": "guide"},
                    },
                ],
            }
        ],
    )

    async def run(*args, **kwargs):
        return {"status": "SUCCEEDED", "result": {"final_text": "answer"}, "trajectory": []}

    monkeypatch.setattr(evaluation, "fixture_run", run)
    report = await evaluation.evaluate(project, mode="fixture")
    assert report["status"] == "FAILED"
    assert [a["passed"] for a in report["cases"][0]["assertions"]] == [True, False]


async def test_dataset_changes_fail_the_report(project, monkeypatch):
    async def run(*args, **kwargs):
        path = project / "evals/document_reader/cases.jsonl"
        path.write_text(path.read_text() + "\n")
        return {"status": "SUCCEEDED", "result": {"final_text": "Completed."}, "files": []}

    monkeypatch.setattr(evaluation, "fixture_run", run)
    report = await evaluation.evaluate(project, mode="fixture")
    assert report["status"] == "FAILED"
    assert report["integrity_errors"] == ["dataset_changed"]


async def test_local_mode_requires_explicit_model_configuration(project):
    with pytest.raises(ValueError, match="explicit"):
        await evaluation.evaluate(project, mode="local")


async def test_unknown_outcome_never_passes_even_when_text_matches(project, monkeypatch):
    async def run(*args, **kwargs):
        return {"status": "OUTCOME_UNKNOWN", "result": {"final_text": "Completed."}}

    monkeypatch.setattr(evaluation, "fixture_run", run)
    report = await evaluation.evaluate(project, mode="fixture")
    assert report["counts"]["error"] == 1
    assert report["cases"][0]["passed"] is False
