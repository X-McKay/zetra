import json

import pytest

from zetra.eval_experiments import load_experiment
from zetra.scaffold import scaffold


def plan(*, trials=3, threshold=0.8):
    return {
        "schema_version": 1,
        "dataset": "live.jsonl",
        "trials": trials,
        "seed": 17,
        "confidence": 0.95,
        "min_case_success_lower_bound": threshold,
        "max_regression": 0.05,
        "required_tags": ["outcome", "trajectory"],
        "limits": {
            "max_invocations": 100,
            "model_calls_per_trial": 8,
            "trial_timeout_seconds": 30,
            "max_duration_seconds": 300,
            "max_total_tokens": 10000,
        },
    }


def live_case(name="guide"):
    return {
        "schema_version": 3,
        "id": name,
        "input": "Read the guide.",
        "tags": ["outcome", "trajectory"],
        "cluster": "guide-document",
        "assertions": [
            {"type": "text_contains", "value": "answer"},
            {"type": "max_total_tokens", "maximum": 1000},
        ],
    }


@pytest.fixture
def experiment_project(tmp_path):
    root = tmp_path / "project"
    scaffold(root, "experiment_reader")
    directory = root / "evals/document_reader"
    (directory / "experiments").mkdir()
    (directory / "live.jsonl").write_text(json.dumps(live_case()) + "\n")
    (directory / "experiments/live.json").write_text(json.dumps(plan()))
    return root


def save_plan(root, data):
    (root / "evals/document_reader/experiments/live.json").write_text(json.dumps(data))


def test_experiment_validation_preserves_case_metadata_without_importing_agent(experiment_project):
    package = experiment_project / "src/experiment_reader/__init__.py"
    package.write_text('raise AssertionError("must not import")')
    loaded = load_experiment(experiment_project, "document_reader", "live")
    assert loaded.plan.trials == 3
    assert loaded.cases[0]["tags"] == ["outcome", "trajectory"]
    assert loaded.cases[0]["cluster"] == "guide-document"


@pytest.mark.parametrize(
    "change",
    [
        {"trials": True},
        {"trials": 0},
        {"trials": 1001},
        {"confidence": 1},
        {"confidence": float("nan")},
        {"dataset": "../../secret.jsonl"},
        {"required_tags": ["missing"]},
        {"required_tags": ["outcome", "outcome"]},
        {"extra": "unknown"},
        {"trials": 101},
    ],
)
def test_invalid_experiment_cannot_start(experiment_project, change):
    save_plan(experiment_project, {**plan(), **change})
    with pytest.raises(ValueError):
        load_experiment(experiment_project, "document_reader", "live")


def test_duplicate_experiment_key_rejected(experiment_project):
    path = experiment_project / "evals/document_reader/experiments/live.json"
    path.write_text(path.read_text().replace('"trials": 3', '"trials": 3, "trials": 4'))
    with pytest.raises(ValueError, match="duplicate"):
        load_experiment(experiment_project, "document_reader", "live")


def test_symlink_dataset_rejected(experiment_project, tmp_path):
    path = experiment_project / "evals/document_reader/live.jsonl"
    target = tmp_path / "outside.jsonl"
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        load_experiment(experiment_project, "document_reader", "live")


@pytest.mark.parametrize("name", ["../live", "/live", "live.json", "", "-x"])
def test_experiment_name_cannot_escape_directory(experiment_project, name):
    with pytest.raises(ValueError):
        load_experiment(experiment_project, "document_reader", name)
