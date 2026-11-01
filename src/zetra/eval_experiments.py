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
