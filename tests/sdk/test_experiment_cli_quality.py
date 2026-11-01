# ruff: noqa: F811 -- imported pytest fixtures are intentionally injected by name
import json
from unittest.mock import AsyncMock

import pytest
from test_eval_comparison import good_report  # noqa: F401
from test_eval_experiments import MODEL, experiment_project, live_case, observation, plan  # noqa: F401
from test_quality import evaluation, project, save

from zetra import cli
from zetra.quality import explain_quality, run_quality


def quality_project(root):
    contract = project(root)
    contract.update(schema_version=2, experiments=["live"])
    save(root, contract)
    directory = root / "evals/reader"
    (directory / "experiments").mkdir()
    (directory / "experiments/live.json").write_text(json.dumps(plan(threshold=0)))
    (directory / "live.jsonl").write_text(json.dumps(live_case()) + "\n")
    return contract


def test_quality_explains_live_plan_without_importing_factory(tmp_path):
    quality_project(tmp_path)
    result = explain_quality(tmp_path)
    assert result["status"] == "PASSED", result
    assert result["experiments"][0]["name"] == "live"
    assert result["experiments"][0]["planned_invocations"] == 3
    assert result["experiments"][0]["model_request_ceiling"] == 24
    assert result["experiments"][0]["all_pass_lower_bound"] < 0.44


def test_bad_declared_live_plan_blocks_offline_static_quality(tmp_path):
    quality_project(tmp_path)
    (tmp_path / "evals/reader/live.jsonl").unlink()
    assert explain_quality(tmp_path)["status"] == "FAILED"


@pytest.mark.parametrize(
    "mode,experiment", [("fixture", "live"), ("local", None), ("local", "undeclared")]
)
async def test_invalid_experiment_quality_combination_never_runs_tests(
    tmp_path, monkeypatch, mode, experiment
):
    quality_project(tmp_path)
    monkeypatch.setattr("zetra.quality._run_pytest", lambda *a: pytest.fail("must not run tests"))
    result = await run_quality(
        tmp_path, mode=mode, experiment=experiment, model_config=MODEL if mode == "local" else None
    )
    assert result["status"] == "FAILED"
    assert result["pytest"] is None


async def test_explicit_live_quality_keeps_deterministic_and_model_evidence_separate(
    tmp_path, monkeypatch
):
    contract = quality_project(tmp_path)
    contract["live_model_required"] = True
    save(tmp_path, contract)
    monkeypatch.setattr("zetra.evaluation.evaluate", AsyncMock(return_value=evaluation()))

    async def run(*args, **kwargs):
        result = observation()
        result["tools"] = {}
        return result

    monkeypatch.setattr("zetra.local.local_run", run)
    result = await run_quality(tmp_path, mode="local", experiment="live", model_config=MODEL)
    assert result["status"] == "PASSED", result
    assert result["evaluation"]["counts"]["planned"] == 1
    assert result["experiment"]["counts"]["planned"] == 3
    assert result["pytest"]["returncode"] == 0


async def test_fixture_gate_does_not_claim_live_evidence(tmp_path, monkeypatch):
    quality_project(tmp_path)
    monkeypatch.setattr("zetra.evaluation.evaluate", AsyncMock(return_value=evaluation()))
    result = await run_quality(tmp_path)
    assert result["status"] == "PASSED"
    assert result["experiment"] is None


@pytest.mark.parametrize("status", ["INCOMPLETE", "INCONCLUSIVE", "FAILED"])
def test_cli_nonpassing_experiment_status_is_nonzero(monkeypatch, status, capsys):
    monkeypatch.setattr(cli, "execute", AsyncMock(return_value={"status": status}))
    result = cli.main(
        [
            "eval",
            "unused",
            "--mode",
            "local",
            "--experiment",
            "live",
            "--model-config",
            "unused.json",
        ]
    )
    assert result == 1
    assert json.loads(capsys.readouterr().out)["status"] == status


async def test_cli_named_experiment_dispatches_shared_runtime(
    experiment_project, monkeypatch, tmp_path
):
    config = tmp_path / "model.json"
    config.write_text(json.dumps(MODEL))

    async def run(*args, **kwargs):
        return observation()

    monkeypatch.setattr("zetra.local.local_run", run)
    args = cli.parser().parse_args(
        [
            "eval",
            str(experiment_project),
            "--mode",
            "local",
            "--experiment",
            "live",
            "--model-config",
            str(config),
        ]
    )
    result = await cli.execute(args, None)
    assert result["status"] == "INCONCLUSIVE"
    assert result["counts"]["planned"] == 3


def test_existing_report_is_refused_before_inference(tmp_path, monkeypatch, capsys):
    output = tmp_path / "existing.json"
    output.write_text("keep")

    async def run(*args):
        pytest.fail("must not start a paid evaluation")

    monkeypatch.setattr(cli, "execute", run)
    assert (
        cli.main(
            [
                "eval",
                "unused",
                "--mode",
                "local",
                "--experiment",
                "live",
                "--model-config",
                "unused",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert output.read_text() == "keep"


def test_new_experiment_report_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "execute", AsyncMock(return_value={"status": "INCONCLUSIVE"}))
    output = tmp_path / "private.json"
    assert (
        cli.main(
            [
                "eval",
                "unused",
                "--mode",
                "local",
                "--experiment",
                "live",
                "--model-config",
                "unused",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert output.stat().st_mode & 0o077 == 0


def test_model_config_rejects_duplicate_keys(tmp_path):
    path = tmp_path / "model.json"
    path.write_text('{"model":"a","model":"b"}')
    with pytest.raises(ValueError, match="duplicate"):
        cli._model_config(path)


def test_cli_compare_reads_and_recomputes_private_artifacts(good_report, tmp_path, capsys):
    baseline = tmp_path / "baseline.json"
    candidate = tmp_path / "candidate.json"
    baseline.write_text(json.dumps(good_report))
    candidate.write_text(json.dumps(good_report))
    assert cli.main(["eval-compare", str(baseline), str(candidate)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "INCONCLUSIVE"


async def test_quality_sigterm_preserves_partial_live_report(experiment_project, tmp_path):
    import asyncio
    import signal
    import sys

    from aiohttp import web

    quality = experiment_project / "evals/document_reader/quality.json"
    contract = json.loads(quality.read_text())
    contract.update(schema_version=2, experiments=["live"])
    quality.write_text(json.dumps(contract))
    started = asyncio.Event()
    block = asyncio.Event()

    async def handle(request):
        started.set()
        await block.wait()
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app, shutdown_timeout=0.1)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    config = tmp_path / "model.json"
    config.write_text(json.dumps({"base_url": f"http://127.0.0.1:{port}/v1", "model": "test"}))
    output = tmp_path / "partial.json"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "zetra",
        "quality",
        str(experiment_project),
        "--mode",
        "local",
        "--experiment",
        "live",
        "--model-config",
        str(config),
        "--output",
        str(output),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(started.wait(), 30)
        process.send_signal(signal.SIGTERM)
        _, stderr = await asyncio.wait_for(process.communicate(), 15)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        block.set()
        await runner.cleanup()
    assert output.exists(), (
        f"No partial evidence; returncode={process.returncode}, stderr={stderr.decode()}"
    )
    report = json.loads(output.read_text())
    assert process.returncode == 1
    assert report["status"] == "FAILED"
    assert report["experiment"]["status"] == "INCOMPLETE"
    assert report["experiment"]["stop_reason"] == "canceled"
