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


MODEL = {"base_url": "http://127.0.0.1:8000/v1", "model": "synthetic-test-model"}


def observation():
    return {
        "status": "SUCCEEDED",
        "result": {"final_text": "answer sensitive-output"},
        "tools": {"read_document": "workspace_read"},
        "trajectory": [],
        "metrics": {"steps": 3, "duration_ms": 20.0, "input_tokens": 60, "output_tokens": 40},
    }


async def test_all_pass_small_sample_is_inconclusive_and_report_omits_raw_outputs(
    experiment_project, monkeypatch
):
    from zetra.eval_experiments import run_experiment

    seen = []

    async def run(root, message, **options):
        seen.append((message, options["model_call_limit"]))
        return observation()

    monkeypatch.setattr("zetra.local.local_run", run)
    report = await run_experiment(
        experiment_project, agent="document_reader", experiment="live", model_config=MODEL
    )
    assert report["status"] == "INCONCLUSIVE"
    assert report["counts"] == {"planned": 3, "passed": 3, "failed": 0, "error": 0, "not_run": 0}
    assert report["summary"]["case_mean_success"] == 1
    assert report["summary"]["cases"][0]["interval"]["lower"] == pytest.approx(0.438503, abs=1e-6)
    assert seen == [("Read the guide.", 8)] * 3
    assert "sensitive-output" not in json.dumps(report)
    assert "Read the guide." not in json.dumps(report)


async def test_observed_budget_stop_accounts_for_unexecuted_trials(experiment_project, monkeypatch):
    from zetra.eval_experiments import run_experiment

    configuration = plan(threshold=0)
    configuration["limits"]["max_total_tokens"] = 100
    save_plan(experiment_project, configuration)

    async def run(*args, **kwargs):
        return observation()

    monkeypatch.setattr("zetra.local.local_run", run)
    report = await run_experiment(
        experiment_project, agent="document_reader", experiment="live", model_config=MODEL
    )
    assert report["status"] == "INCOMPLETE"
    assert report["counts"] == {"planned": 3, "passed": 1, "failed": 0, "error": 0, "not_run": 2}
    assert report["stop_reason"] == "token_budget"
    assert report["summary"]["metrics"]["input_tokens"]["observed"] == 1
    assert report["summary"]["cases"][0]["interval"] is None


async def test_missing_usage_cannot_satisfy_requested_budget(experiment_project, monkeypatch):
    from zetra.eval_experiments import run_experiment

    async def run(*args, **kwargs):
        result = observation()
        result["metrics"]["input_tokens"] = None
        return result

    monkeypatch.setattr("zetra.local.local_run", run)
    report = await run_experiment(
        experiment_project, agent="document_reader", experiment="live", model_config=MODEL
    )
    assert report["status"] == "INCOMPLETE"
    assert report["stop_reason"] == "usage_unavailable"
    assert report["counts"]["not_run"] == 2
    assert report["counts"]["failed"] == 1
    assert report["summary"]["metrics"]["input_tokens"]["total"] is None


async def test_provider_exceptions_are_errors_without_secret_details_or_fallback(
    experiment_project, monkeypatch
):
    from zetra.eval_experiments import run_experiment

    configuration = plan()
    configuration["limits"]["max_total_tokens"] = None
    save_plan(experiment_project, configuration)

    async def run(*args, **kwargs):
        raise RuntimeError("provider-secret")

    monkeypatch.setattr("zetra.local.local_run", run)
    report = await run_experiment(
        experiment_project, agent="document_reader", experiment="live", model_config=MODEL
    )
    assert report["status"] == "FAILED"
    assert report["counts"]["error"] == 3
    assert "provider-secret" not in json.dumps(report)


async def test_changed_dataset_stops_further_trials(experiment_project, monkeypatch):
    from zetra.eval_experiments import run_experiment

    async def run(*args, **kwargs):
        path = experiment_project / "evals/document_reader/live.jsonl"
        path.write_text(path.read_text() + "\n")
        return observation()

    monkeypatch.setattr("zetra.local.local_run", run)
    report = await run_experiment(
        experiment_project, agent="document_reader", experiment="live", model_config=MODEL
    )
    assert report["status"] == "FAILED"
    assert report["integrity_errors"]
    assert report["counts"]["not_run"] == 2


async def test_cancellation_returns_partial_nonpassing_evidence(experiment_project, monkeypatch):
    import asyncio

    from zetra.eval_experiments import run_experiment

    async def run(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("zetra.local.local_run", run)
    report = await run_experiment(
        experiment_project, agent="document_reader", experiment="live", model_config=MODEL
    )
    assert report["status"] == "INCOMPLETE"
    assert report["stop_reason"] == "canceled"
    assert report["counts"]["error"] == 1
    assert report["counts"]["not_run"] == 2


async def test_deadline_does_not_start_a_trial_without_cleanup_allowance(experiment_project):
    from zetra.eval_experiments import run_experiment

    configuration = plan()
    configuration["limits"]["max_duration_seconds"] = 1
    save_plan(experiment_project, configuration)
    report = await run_experiment(
        experiment_project, agent="document_reader", experiment="live", model_config=MODEL
    )
    assert report["status"] == "INCOMPLETE"
    assert report["stop_reason"] == "duration_budget"
    assert report["counts"]["not_run"] == 3


async def test_exact_budget_on_last_trial_is_not_an_exhausted_schedule(
    experiment_project, monkeypatch
):
    from zetra.eval_experiments import run_experiment

    configuration = plan(threshold=0)
    configuration["limits"]["max_total_tokens"] = 300
    save_plan(experiment_project, configuration)

    async def run(*args, **kwargs):
        return observation()

    monkeypatch.setattr("zetra.local.local_run", run)
    report = await run_experiment(
        experiment_project, agent="document_reader", experiment="live", model_config=MODEL
    )
    assert report["status"] == "PASSED"
    assert report["stop_reason"] is None


async def test_multiple_cases_widen_intervals_instead_of_pooling_trials(
    experiment_project, monkeypatch
):
    from zetra.eval_experiments import run_experiment

    path = experiment_project / "evals/document_reader/live.jsonl"
    path.write_text("\n".join(json.dumps(live_case(name)) for name in ["a", "b"]) + "\n")

    async def run(*args, **kwargs):
        return observation()

    monkeypatch.setattr("zetra.local.local_run", run)
    report = await run_experiment(
        experiment_project, agent="document_reader", experiment="live", model_config=MODEL
    )
    assert report["summary"]["distinct_cases"] == 2
    assert report["summary"]["distinct_clusters"] == 1
    assert report["summary"]["trials_per_case"] == 3
    assert report["summary"]["per_case_confidence"] == 0.975
    assert report["summary"]["cases"][0]["interval"]["lower"] < 0.4385


async def test_repeated_http_model_trials_use_fresh_workspaces(experiment_project):
    from aiohttp import web

    from zetra.eval_experiments import run_experiment

    package = experiment_project / "src/experiment_reader/agents/document_reader/factory.py"
    package.write_text('''from google.adk.agents import LlmAgent
from zetra import Tool

def create_agent(ctx):
    async def read_document(document_id: str):
        """Read a guide in fresh state."""
        marker = ctx.workspace / 'counter.txt'
        count = int(marker.read_text()) + 1 if marker.exists() else 1
        marker.write_text(str(count))
        return {'count':count}
    return LlmAgent(name='document_reader', model=ctx.model, instruction=ctx.instruction,
                    tools=ctx.tools(Tool(read_document,'workspace_read')))
''')
    requests = []

    async def handle(request):
        body = await request.json()
        requests.append(body)
        tool = next((m for m in body["messages"] if m["role"] == "tool"), None)
        message = (
            {"role": "assistant", "content": "answer"}
            if tool
            else {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "read",
                        "type": "function",
                        "function": {
                            "name": "read_document",
                            "arguments": '{"document_id":"guide"}',
                        },
                    }
                ],
            }
        )
        return web.json_response(
            {
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop" if tool else "tool_calls",
                        "message": message,
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    save_plan(experiment_project, plan(threshold=0))
    try:
        report = await run_experiment(
            experiment_project,
            agent="document_reader",
            experiment="live",
            model_config={"base_url": f"http://127.0.0.1:{port}/v1", "model": "test"},
        )
    finally:
        await runner.cleanup()
    assert report["status"] == "PASSED", report
    assert len(requests) == 6
    results = [
        json.loads(m["content"]) for r in requests for m in r["messages"] if m["role"] == "tool"
    ]
    assert results == [{"count": 1}] * 3
    assert report["summary"]["metrics"]["input_tokens"]["total"] == 60
