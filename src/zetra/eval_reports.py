"""Compact, bounded experiment evidence and reproducible statistical decisions."""

import hashlib
import platform
from importlib import metadata
from pathlib import Path
from statistics import mean
from typing import Annotated, Literal

from pydantic import Field, FiniteFloat, model_serializer, model_validator

from ._runtime.model_diagnostics import ModelFailureEvidence
from .eval_experiments import ExperimentPlan, Strict
from .eval_statistics import metric_summary, wilson
from .local import LocalModelConfig
from .manifest import digest

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Label = Annotated[str, Field(min_length=1, max_length=128)]
MetricInt = Annotated[int, Field(ge=0, le=1_000_000_000)]


class Metrics(Strict):
    steps: Annotated[int, Field(ge=0, le=64)] | None = None
    duration_ms: Annotated[FiniteFloat, Field(ge=0, le=86_400_000)] | None = None
    input_tokens: MetricInt | None = None
    output_tokens: MetricInt | None = None


class AssertionEvidence(Strict):
    type: Label
    passed: bool


class TrialEvidence(Strict):
    trial: int = Field(ge=0, le=999)
    outcome: Literal["passed", "failed", "error", "not_run"]
    status: Annotated[str, Field(pattern=r"^[A-Z_]{1,64}$")] | None = None
    error_type: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")] | None = None
    assertions: list[AssertionEvidence] = Field(max_length=100)
    tools: dict[Label, Literal["workspace_read", "workspace_write", "remote_read"]] = Field(
        max_length=100
    )
    metrics: Metrics
    diagnostic: ModelFailureEvidence | None = None

    @model_serializer(mode="wrap")
    def omit_absent_diagnostic(self, handler):
        data = handler(self)
        if self.diagnostic is None:
            data.pop("diagnostic", None)
        return data

    @model_validator(mode="after")
    def coherent_outcome(self):
        if self.diagnostic is not None and (
            self.outcome != "error" or self.status != "OUTCOME_UNKNOWN"
        ):
            raise ValueError("model diagnostic requires an unknown error outcome")
        if self.outcome in {"passed", "failed"}:
            if self.status != "SUCCEEDED" or not self.assertions or self.error_type is not None:
                raise ValueError("inconsistent completed trial")
            if (self.outcome == "passed") != all(row.passed for row in self.assertions):
                raise ValueError("trial outcome differs from assertions")
        elif self.outcome == "not_run":
            if (
                self.status is not None
                or self.error_type is not None
                or self.assertions
                or self.tools
                or any(v is not None for v in self.metrics.model_dump().values())
            ):
                raise ValueError("unexecuted trial contains observed evidence")
        elif self.status == "SUCCEEDED":
            raise ValueError("error trial cannot claim successful execution")
        return self


class CaseEvidence(Strict):
    id: Label
    case_sha256: Hash
    tags: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        min_length=1, max_length=32
    )
    cluster: Label
    assertion_types: list[Label] = Field(min_length=1, max_length=100)
    trials: list[TrialEvidence] = Field(min_length=1, max_length=1000)


class ExperimentEvidence(Strict):
    schema_version: Literal[3]
    mode: Literal["local"]
    agent: Label
    experiment: Label
    plan: ExperimentPlan
    plan_sha256: Hash
    plan_file_sha256: Hash
    dataset_sha256: Hash
    source_sha256: Hash
    agent_sha256: Hash
    evaluator_sha256: Hash
    model_binding: LocalModelConfig = Field(alias="model_config")
    model_config_sha256: Hash
    mcp_binding: dict
    created_at: Annotated[str, Field(min_length=1, max_length=64)]
    trust: Literal["local-unsigned"]
    scope: Literal[
        "fixed development cases on a trusted local model endpoint; not production qualification"
    ]
    cases: list[CaseEvidence] = Field(min_length=1, max_length=1000)
    stop_reason: (
        Literal[
            "token_budget", "usage_unavailable", "duration_budget", "canceled", "inputs_changed"
        ]
        | None
    )
    integrity_errors: list[Literal["inputs_changed_or_unreadable"]] = Field(max_length=1)

    @model_validator(mode="after")
    def coherent_schedule(self):
        if self.plan_sha256 != digest(self.plan.model_dump()) or self.model_config_sha256 != digest(
            self.model_binding.model_dump()
        ):
            raise ValueError("configuration digest mismatch")
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("duplicate case identity")
        if len(self.cases) * self.plan.trials > self.plan.limits.max_invocations:
            raise ValueError("report exceeds planned invocations")
        if set(self.plan.required_tags) - {tag for case in self.cases for tag in case.tags}:
            raise ValueError("missing required tag")
        # Binding has an intentionally small public representation; validate against
        # the same helper as execution, without instantiating a fixture provider.
        binding = self.mcp_binding
        if binding != {"mode": "unbound", "sha256": None} and not (
            set(binding) == {"mode", "sha256"}
            and binding["mode"] == "fixture"
            and isinstance(binding["sha256"], str)
            and len(binding["sha256"]) == 64
            and all(c in "0123456789abcdef" for c in binding["sha256"])
        ):
            raise ValueError("invalid MCP binding identity")
        for case in self.cases:
            if len(set(case.tags)) != len(case.tags):
                raise ValueError("duplicate tag")
            if [trial.trial for trial in case.trials] != list(range(self.plan.trials)):
                raise ValueError("incomplete or duplicate trial schedule")
            for trial in case.trials:
                if (
                    trial.outcome in {"passed", "failed"}
                    and [row.type for row in trial.assertions] != case.assertion_types
                ):
                    raise ValueError("trial assertions differ from planned case")
        return self


def evaluator_digest():
    """Identity of installed evaluator/runner Python, independent of agent source."""
    root = Path(__file__).parent
    names = [
        "eval_cases.py",
        "eval_statistics.py",
        "eval_experiments.py",
        "eval_reports.py",
        "eval_comparison.py",
        "evaluation.py",
        "local.py",
        "agent.py",
        "bindings.py",
        "manifest.py",
        "testing/local.py",
    ]
    names += [p.relative_to(root).as_posix() for p in (root / "_runtime").rglob("*.py")]
    return digest(
        {
            "files": {
                name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for name in sorted(names)
            },
            "python": platform.python_version(),
            "dependencies": {
                name: metadata.version(name)
                for name in ("google-adk", "google-genai", "litellm", "openai", "pydantic", "httpx")
            },
        }
    )


def summarize(evidence):
    plan = evidence.plan
    per_case_confidence = 1 - (1 - plan.confidence) / len(evidence.cases)
    counts = {name: 0 for name in ("planned", "passed", "failed", "error", "not_run")}
    summaries = []
    observed = []
    for case in evidence.cases:
        successes = sum(t.outcome == "passed" for t in case.trials)
        complete = all(t.outcome != "not_run" for t in case.trials)
        for trial in case.trials:
            counts["planned"] += 1
            counts[trial.outcome] += 1
            if trial.outcome != "not_run":
                observed.append(trial.metrics)
        summaries.append(
            {
                "id": case.id,
                "passed": successes,
                "planned": plan.trials,
                "pass_rate": successes / plan.trials,
                "all_trials_passed": successes == plan.trials,
                "at_least_one_passed": successes > 0,
                "interval": wilson(successes, plan.trials, per_case_confidence)
                if complete
                else None,
            }
        )
    metrics = {
        name: metric_summary([getattr(row, name) for row in observed])
        for name in Metrics.model_fields
    }
    metrics["total_tokens"] = metric_summary(
        [
            row.input_tokens + row.output_tokens
            if row.input_tokens is not None and row.output_tokens is not None
            else None
            for row in observed
        ]
    )
    slices = {}
    for field in ("tags", "cluster"):
        groups = {}
        for case, row in zip(evidence.cases, summaries, strict=True):
            for key in case.tags if field == "tags" else [case.cluster]:
                groups.setdefault(key, []).append(row)
        slices[field] = {
            key: {
                "cases": len(rows),
                "case_mean_success": mean(r["pass_rate"] for r in rows),
                "all_trials_passed": sum(r["all_trials_passed"] for r in rows),
            }
            for key, rows in sorted(groups.items())
        }
    summary = {
        "distinct_cases": len(summaries),
        "distinct_clusters": len(slices["cluster"]),
        "trials_per_case": plan.trials,
        "case_mean_success": mean(row["pass_rate"] for row in summaries),
        "all_trials_passed_fraction": mean(row["all_trials_passed"] for row in summaries),
        "at_least_one_passed_fraction": mean(row["at_least_one_passed"] for row in summaries),
        "purpose": "reliability" if plan.min_case_success_lower_bound else "smoke",
        "interval_method": "wilson-two-sided-bonferroni-by-case",
        "family_confidence": plan.confidence,
        "per_case_confidence": per_case_confidence,
        "interval_assumption": "independent stable draws for each fixed case; no unseen-task or suite-mean interval",
        "percentile_method": "nearest-rank over all executed trials, including failures",
        "cases": summaries,
        "slices": slices,
        "metrics": metrics,
    }
    if evidence.integrity_errors:
        status = "FAILED"
    elif counts["not_run"]:
        status = "INCOMPLETE"
    elif counts["failed"] or counts["error"] or evidence.stop_reason:
        status = "FAILED"
    elif any(row["interval"]["lower"] < plan.min_case_success_lower_bound for row in summaries):
        status = "INCONCLUSIVE"
    else:
        status = "PASSED"
    return {"status": status, "counts": counts, "summary": summary}


def finalize_report(data):
    evidence = ExperimentEvidence.model_validate(data)
    return {**evidence.model_dump(by_alias=True), **summarize(evidence)}


def validate_report(data):
    if type(data) is not dict:
        raise ValueError("expected experiment report")
    fields = {"status", "counts", "summary"}
    if not fields <= data.keys():
        raise ValueError("missing report summary")
    expected = finalize_report({k: v for k, v in data.items() if k not in fields})
    if digest(expected) != digest(data):
        raise ValueError("report differs from recomputed evidence")
    return expected
