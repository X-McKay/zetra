# ruff: noqa: F811 -- imported pytest fixture is intentionally injected by name
import copy

import pytest
from test_eval_experiments import MODEL, experiment_project, observation, plan, save_plan  # noqa: F401

from zetra.eval_comparison import compare_reports, read_report
from zetra.eval_experiments import run_experiment
from zetra.manifest import digest


@pytest.fixture
async def good_report(experiment_project, monkeypatch):
    save_plan(experiment_project, plan(threshold=0))

    async def run(*args, **kwargs):
        return observation()

    monkeypatch.setattr("zetra.local.local_run", run)
    return await run_experiment(
        experiment_project, agent="document_reader", experiment="live", model_config=MODEL
    )


def test_identical_small_samples_do_not_prove_noninferiority(good_report):
    result = compare_reports(good_report, good_report)
    assert result["status"] == "INCONCLUSIVE"
    assert result["case_mean_delta"] == 0
    assert result["cases"][0]["delta"] == 0
    assert result["cases"][0]["interval"]["lower"] < -0.5
    assert result["cases"][0]["interval"]["upper"] > 0.5


async def test_repeated_case_sample_can_demonstrate_predeclared_margin(
    experiment_project, monkeypatch
):
    configuration = plan(trials=100, threshold=0.8)
    configuration["limits"]["max_total_tokens"] = 20000
    save_plan(experiment_project, configuration)

    async def run(*args, **kwargs):
        return observation()

    monkeypatch.setattr("zetra.local.local_run", run)
    report = await run_experiment(
        experiment_project, agent="document_reader", experiment="live", model_config=MODEL
    )
    result = compare_reports(report, report)
    assert result["status"] == "PASSED"
    assert result["cases"][0]["interval"]["lower"] > -0.05
    assert result["distinct_cases"] == 1


@pytest.mark.parametrize(
    "field", ["dataset_sha256", "evaluator_sha256", "agent_sha256", "mcp_binding"]
)
def test_incompatible_comparison_rejected(good_report, field):
    candidate = copy.deepcopy(good_report)
    candidate[field] = (
        {"mode": "fixture", "sha256": "a" * 64} if field == "mcp_binding" else "a" * 64
    )
    with pytest.raises(ValueError, match="incompatible"):
        compare_reports(good_report, candidate)


def test_source_and_model_may_change_but_are_exposed(good_report):
    candidate = copy.deepcopy(good_report)
    candidate["source_sha256"] = "a" * 64
    candidate["model_config"]["model"] = "candidate"
    candidate["model_config_sha256"] = digest(candidate["model_config"])
    result = compare_reports(good_report, candidate)
    assert result["candidate"]["source_sha256"] == "a" * 64
    assert result["candidate"]["model_config"]["model"] == "candidate"


@pytest.mark.parametrize(
    "change", ["counts", "duplicate", "boolean", "summary", "raw", "nan", "missing"]
)
def test_forged_or_malformed_report_never_passes_comparison(good_report, change):
    candidate = copy.deepcopy(good_report)
    if change == "counts":
        candidate["counts"]["passed"] = 999
    elif change == "duplicate":
        candidate["cases"][0]["trials"][1]["trial"] = 0
    elif change == "boolean":
        candidate["cases"][0]["trials"][0]["metrics"]["input_tokens"] = True
    elif change == "summary":
        candidate["status"] = "FAILED"
    elif change == "raw":
        candidate["cases"][0]["trials"][0]["prompt"] = "secret"
    elif change == "nan":
        candidate["cases"][0]["trials"][0]["metrics"]["duration_ms"] = float("nan")
    else:
        candidate["cases"][0]["trials"].pop()
    with pytest.raises(ValueError):
        compare_reports(good_report, candidate)


async def test_observed_failure_cannot_be_averaged_away(
    good_report, experiment_project, monkeypatch
):
    async def run(*args, **kwargs):
        value = observation()
        value["result"]["final_text"] = "wrong"
        return value

    monkeypatch.setattr("zetra.local.local_run", run)
    candidate = await run_experiment(
        experiment_project, agent="document_reader", experiment="live", model_config=MODEL
    )
    result = compare_reports(good_report, candidate)
    assert result["status"] == "FAILED"
    assert result["regressed"] == ["guide"]


def test_report_reader_rejects_duplicate_keys_before_comparison(tmp_path):
    path = tmp_path / "report.json"
    path.write_text('{"status":"PASSED","status":"FAILED"}')
    with pytest.raises(ValueError, match="duplicate"):
        read_report(path)
