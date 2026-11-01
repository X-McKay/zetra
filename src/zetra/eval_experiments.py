"""Data-only experiment plans and bounded local-model trials."""

import json
import re
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator

from .eval_cases import _unique_object, load_cases
from .manifest import project_file, relative_path

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)


class ExperimentLimits(Strict):
    max_invocations: int = Field(ge=1, le=2000)
    model_calls_per_trial: int = Field(ge=1, le=32)
    trial_timeout_seconds: FiniteFloat = Field(ge=1, le=600)
    max_duration_seconds: FiniteFloat = Field(ge=1, le=86400)
    max_total_tokens: int | None = Field(ge=1, le=1_000_000_000)


class ExperimentPlan(Strict):
    schema_version: Literal[1]
    dataset: str
    trials: int = Field(ge=1, le=1000)
    seed: int = Field(ge=0, le=2**32 - 1)
    confidence: FiniteFloat = Field(ge=0.8, le=0.999)
    min_case_success_lower_bound: FiniteFloat = Field(ge=0, le=1)
    max_regression: FiniteFloat = Field(ge=0, le=1)
    required_tags: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        min_length=1, max_length=32
    )
    limits: ExperimentLimits

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema version must be an integer")
        return value

    @field_validator("dataset")
    @classmethod
    def dataset_path(cls, value):
        if relative_path(value).suffix != ".jsonl":
            raise ValueError("experiment dataset must be JSONL")
        return value

    @field_validator("required_tags")
    @classmethod
    def unique_tags(cls, value):
        if len(set(value)) != len(value) or any(not tag.strip() for tag in value):
            raise ValueError("required tags must be unique and nonempty")
        return value


@dataclass(frozen=True)
class LoadedExperiment:
    name: str
    plan: ExperimentPlan
    cases: list[dict]
    plan_raw: bytes
    dataset_raw: bytes


def load_experiment(root, agent, name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("invalid experiment name")
    if len(relative_path(agent).parts) != 1:
        raise ValueError("invalid agent name")
    path = project_file(root, f"evals/{agent}/experiments/{name}.json")
    with path.open("rb") as stream:
        raw = stream.read(65_537)
    if len(raw) > 65_536:
        raise ValueError("experiment plan too large")
    try:
        plan = ExperimentPlan.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid experiment encoding or nesting") from exc
    cases, dataset = load_cases(root, agent, dataset=plan.dataset)
    if len(cases) * plan.trials > plan.limits.max_invocations:
        raise ValueError("planned trials exceed invocation budget")
    if any(case["schema_version"] != 3 for case in cases):
        raise ValueError("live experiments require version-3 tagged cases")
    tags = {tag for case in cases for tag in case["tags"]}
    if set(plan.required_tags) - tags:
        raise ValueError("experiment dataset lacks required tags")
    return LoadedExperiment(name, plan, cases, raw, dataset)


async def run_experiment(root, *, agent, experiment, model_config, mcp_fixtures=None):
    """Execute a predeclared schedule; budget stops never shorten the denominator."""
    import asyncio
    import hashlib
    import random
    import time
    from datetime import UTC, datetime
    from pathlib import Path

    from .bindings import MCPFixtures, mcp_provenance
    from .eval_reports import TrialEvidence, evaluator_digest, finalize_report
    from .evaluation import evaluate_case
    from .local import LocalModelConfig
    from .manifest import digest, load_agent, source_digest

    root = Path(root).resolve()
    spec = load_agent(root, agent)
    loaded = load_experiment(root, spec.name, experiment)
    config = LocalModelConfig.model_validate(model_config)
    # Resolve only credential presence, not values, before any agent execution.
    if config.api_key_env:
        import os

        if not os.environ.get(config.api_key_env):
            raise ValueError("configured model credential is unavailable")
    fixtures = MCPFixtures(mcp_fixtures) if mcp_fixtures is not None else None
    plan = loaded.plan
    source = source_digest(root)
    implementation = evaluator_digest()
    evidence = {
        "schema_version": 3,
        "mode": "local",
        "agent": spec.name,
        "experiment": experiment,
        "plan": plan.model_dump(),
        "plan_sha256": digest(plan.model_dump()),
        "plan_file_sha256": hashlib.sha256(loaded.plan_raw).hexdigest(),
        "dataset_sha256": hashlib.sha256(loaded.dataset_raw).hexdigest(),
        "source_sha256": source,
        "agent_sha256": digest(spec.model_dump()),
        "evaluator_sha256": implementation,
        "model_config": config.model_dump(),
        "model_config_sha256": digest(config.model_dump()),
        "mcp_binding": mcp_provenance(fixtures),
        "created_at": datetime.now(UTC).isoformat(),
        "trust": "local-unsigned",
        "scope": "fixed development cases on a trusted local model endpoint; not production qualification",
        "cases": [],
        "stop_reason": None,
        "integrity_errors": [],
    }
    for case in loaded.cases:
        evidence["cases"].append(
            {
                "id": case["id"],
                "case_sha256": digest(case),
                "tags": case["tags"],
                "cluster": case["cluster"],
                "assertion_types": [a["type"] for a in case["assertions"]],
                "trials": [
                    TrialEvidence(
                        trial=i, outcome="not_run", assertions=[], tools={}, metrics={}
                    ).model_dump()
                    for i in range(plan.trials)
                ],
            }
        )
    schedule = []
    rng = random.Random(plan.seed)
    for trial in range(plan.trials):
        indices = list(range(len(loaded.cases)))
        rng.shuffle(indices)
        schedule.extend((index, trial) for index in indices)
    deadline = time.monotonic() + plan.limits.max_duration_seconds
    tokens = 0

    def unchanged():
        try:
            current = load_experiment(root, spec.name, experiment)
            return (
                current.plan_raw == loaded.plan_raw
                and current.dataset_raw == loaded.dataset_raw
                and source_digest(root) == source
                and evaluator_digest() == implementation
            )
        except (ValueError, OSError):
            return False

    def changed():
        evidence["integrity_errors"] = ["inputs_changed_or_unreadable"]
        evidence["stop_reason"] = "inputs_changed"

    for ordinal, (index, trial) in enumerate(schedule):
        if not unchanged():
            changed()
            break
        remaining = deadline - time.monotonic() - 10  # native child teardown allowance
        if remaining < 1:
            evidence["stop_reason"] = "duration_budget"
            break
        try:
            row = await evaluate_case(
                root,
                loaded.cases[index],
                mode="local",
                agent=spec.name,
                model_config=config.model_dump(),
                mcp_fixtures=fixtures.config() if fixtures else None,
                timeout=min(plan.limits.trial_timeout_seconds, remaining),
                model_call_limit=plan.limits.model_calls_per_trial,
            )
            compact = TrialEvidence(
                trial=trial,
                outcome=row["outcome"],
                status=row.get("status"),
                error_type=row.get("error_type"),
                diagnostic=row.get("diagnostic"),
                assertions=[{"type": r["type"], "passed": r["passed"]} for r in row["assertions"]],
                tools=row["tools"],
                metrics=row["metrics"],
            )
        except asyncio.CancelledError:
            compact = TrialEvidence(
                trial=trial,
                outcome="error",
                status="CANCELED",
                error_type="CancelledError",
                assertions=[],
                tools={},
                metrics={},
            )
            evidence["stop_reason"] = "canceled"
        except (ValueError, TypeError, KeyError):
            compact = TrialEvidence(
                trial=trial,
                outcome="error",
                status="ERROR",
                error_type="InvalidEvidence",
                assertions=[],
                tools={},
                metrics={},
            )
        evidence["cases"][index]["trials"][trial] = compact.model_dump()
        if not unchanged():
            changed()
        if evidence["stop_reason"]:
            break
        metrics = compact.metrics
        if plan.limits.max_total_tokens is not None:
            if metrics.input_tokens is None or metrics.output_tokens is None:
                evidence["stop_reason"] = "usage_unavailable"
            else:
                tokens += metrics.input_tokens + metrics.output_tokens
                if tokens > plan.limits.max_total_tokens or (
                    tokens == plan.limits.max_total_tokens and ordinal + 1 < len(schedule)
                ):
                    evidence["stop_reason"] = "token_budget"
        if evidence["stop_reason"]:
            break
    return finalize_report(evidence)
