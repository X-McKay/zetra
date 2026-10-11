from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from agentctl.diagnostics import Diagnostic, emit_diagnostics, relative_display
from agentctl.files import DataFileError, load_data
from agentctl.ui import risk_report

TIERS = ("low", "medium", "high", "critical")
TIER_RANK = {tier: rank for rank, tier in enumerate(TIERS)}
RISK_MATRIX = (
    ("low", "low", "medium", "medium"),
    ("low", "medium", "medium", "high"),
    ("medium", "medium", "high", "critical"),
    ("high", "high", "critical", "critical"),
)
SUPPORTED_SUFFIXES = {".json", ".yaml", ".yml"}


def schema_path() -> Path:
    return Path(str(files("agentctl").joinpath("schemas/risk-assessment.schema.json")))


@lru_cache(maxsize=1)
def load_schema() -> dict[str, Any]:
    schema = json.loads(schema_path().read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def validate_risk_assessments(
    *, root: Path, paths: Sequence[Path], output_format: str = "text"
) -> int:
    root = root.resolve()
    targets = _resolve_targets(root, paths)
    diagnostics: list[Diagnostic] = []
    if not targets:
        diagnostics.append(
            Diagnostic(
                "RISK001",
                "no risk assessments found under docs/risk-assessments",
                str(root),
            )
        )
    for path in targets:
        diagnostics.extend(validate_risk_file(path, root=root))
    return emit_diagnostics(
        diagnostics,
        output_format=output_format,
        heading="Agent risk assessment validation",
    )


def validate_risk_file(path: Path, *, root: Path) -> list[Diagnostic]:
    path = path if path.is_absolute() else root / path
    shown = relative_display(path, root)
    try:
        payload = load_data(path)
    except DataFileError as exc:
        return [Diagnostic("RISK002", str(exc), shown)]

    validator = Draft202012Validator(load_schema(), format_checker=FormatChecker())
    schema_errors = sorted(validator.iter_errors(payload), key=_schema_error_key)
    if schema_errors:
        return [
            Diagnostic(
                "RISK003",
                f"{_json_path(error.absolute_path)}: {error.message}",
                shown,
            )
            for error in schema_errors
        ]
    return _semantic_diagnostics(payload, shown)


def explain_risk_assessment(
    *, root: Path, path: Path, output_format: str = "text"
) -> int:
    root = root.resolve()
    resolved = path if path.is_absolute() else root / path
    diagnostics = validate_risk_file(resolved, root=root)
    errors = [item for item in diagnostics if item.severity == "error"]
    if errors:
        return emit_diagnostics(
            diagnostics,
            output_format=output_format,
            heading="Agent risk assessment",
        )
    payload = load_data(resolved)
    if output_format == "json":
        print(
            json.dumps(
                {
                    "ok": True,
                    "path": relative_display(resolved, root),
                    "assessment": payload,
                    "diagnostics": [asdict(item) for item in diagnostics],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        risk_report(payload, path=relative_display(resolved, root))
        if diagnostics:
            emit_diagnostics(
                diagnostics,
                output_format="text",
                heading="Assessment notices",
            )
    return 0


def _resolve_targets(root: Path, paths: Sequence[Path]) -> list[Path]:
    if paths:
        targets: set[Path] = set()
        for raw in paths:
            path = raw if raw.is_absolute() else root / raw
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
                targets.add(path.resolve())
            elif path.is_dir():
                targets.update(
                    item.resolve()
                    for item in path.rglob("*")
                    if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES
                )
            elif path.suffix.lower() in SUPPORTED_SUFFIXES:
                targets.add(path.resolve())
        return sorted(targets)
    directory = root / "docs" / "risk-assessments"
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.suffix.lower() in SUPPORTED_SUFFIXES
    )


def _semantic_diagnostics(payload: Mapping[str, Any], shown: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    scenarios = payload["scenarios"]
    scenario_ids: set[str] = set()
    inherent_tiers: list[str] = []
    residual_tiers: list[str] = []

    for index, scenario in enumerate(scenarios):
        location = f"scenarios[{index}]"
        scenario_id = scenario["id"]
        if scenario_id in scenario_ids:
            diagnostics.append(
                Diagnostic(
                    "RISK004", f"{location}.id is duplicated: {scenario_id}", shown
                )
            )
        scenario_ids.add(scenario_id)

        control_ids: set[str] = set()
        for control in scenario["controls"]:
            if control["id"] in control_ids:
                diagnostics.append(
                    Diagnostic(
                        "RISK012",
                        f"{location} has duplicate control ID {control['id']}",
                        shown,
                    )
                )
            control_ids.add(control["id"])

        if scenario["primary_dimension"] in scenario["secondary_dimensions"]:
            diagnostics.append(
                Diagnostic(
                    "RISK013",
                    f"{location} repeats its primary dimension as a secondary dimension",
                    shown,
                )
            )

        for rating_name in ("inherent", "residual"):
            rating = scenario[rating_name]
            expected = matrix_tier(rating["impact"], rating["likelihood"])
            if rating["tier"] != expected:
                diagnostics.append(
                    Diagnostic(
                        "RISK005",
                        f"{location}.{rating_name}.tier must be {expected} for "
                        f"impact {rating['impact']} and likelihood {rating['likelihood']}",
                        shown,
                    )
                )

        inherent_tiers.append(scenario["inherent"]["tier"])
        residual_tiers.append(scenario["residual"]["tier"])
        risk_reduced = (
            TIER_RANK[scenario["residual"]["tier"]]
            < TIER_RANK[scenario["inherent"]["tier"]]
            or scenario["residual"]["impact"] < scenario["inherent"]["impact"]
            or scenario["residual"]["likelihood"] < scenario["inherent"]["likelihood"]
        )
        if risk_reduced and not any(
            control["effectiveness"] == "verified" for control in scenario["controls"]
        ):
            diagnostics.append(
                Diagnostic(
                    "RISK011",
                    f"{location} reduces residual risk without a verified control",
                    shown,
                )
            )

    classification = payload["classification"]
    maximum_inherent = max(inherent_tiers, key=TIER_RANK.__getitem__)
    maximum_residual = max(residual_tiers, key=TIER_RANK.__getitem__)
    if classification["maximum_inherent_tier"] != maximum_inherent:
        diagnostics.append(
            Diagnostic(
                "RISK006",
                f"classification.maximum_inherent_tier must be {maximum_inherent}",
                shown,
            )
        )
    if classification["maximum_residual_tier"] != maximum_residual:
        diagnostics.append(
            Diagnostic(
                "RISK007",
                f"classification.maximum_residual_tier must be {maximum_residual}",
                shown,
            )
        )

    floors = [
        item["tier"] for item in payload["regulatory_screen"]["governance_floors"]
    ]
    expected_governance = max([maximum_inherent, *floors], key=TIER_RANK.__getitem__)
    if classification["governance_tier"] != expected_governance:
        diagnostics.append(
            Diagnostic(
                "RISK008",
                f"classification.governance_tier must be {expected_governance}",
                shown,
            )
        )
    if (
        payload["regulatory_screen"]["prohibited_use"]
        and classification["decision"] != "no_go"
    ):
        diagnostics.append(
            Diagnostic("RISK009", "a prohibited use requires decision: no_go", shown)
        )
    if maximum_residual == "critical" and classification["decision"] != "no_go":
        diagnostics.append(
            Diagnostic(
                "RISK010", "critical residual risk requires decision: no_go", shown
            )
        )

    accepted = set(payload["acceptance"]["accepted_scenarios"])
    unknown_accepted = sorted(accepted - scenario_ids)
    if unknown_accepted:
        diagnostics.append(
            Diagnostic(
                "RISK015",
                "acceptance references unknown scenarios: "
                + ", ".join(unknown_accepted),
                shown,
            )
        )

    assessed_at = date.fromisoformat(payload["assessment"]["assessed_at"])
    review_by = date.fromisoformat(payload["assessment"]["review_by"])
    expires_at = date.fromisoformat(payload["acceptance"]["expires_at"])
    if review_by < assessed_at or expires_at < assessed_at:
        diagnostics.append(
            Diagnostic(
                "RISK014",
                "review_by and acceptance.expires_at must not precede assessed_at",
                shown,
            )
        )
    if payload["assessment"]["status"] == "draft":
        diagnostics.append(
            Diagnostic(
                "RISK101",
                "assessment is draft and cannot satisfy production readiness",
                shown,
                severity="warning",
            )
        )
    if payload["open_questions"]:
        diagnostics.append(
            Diagnostic(
                "RISK102",
                f"assessment has {len(payload['open_questions'])} open question(s)",
                shown,
                severity="warning",
            )
        )
    return diagnostics


def matrix_tier(impact: int, likelihood: int) -> str:
    return RISK_MATRIX[impact - 1][likelihood - 1]


def _schema_error_key(error: Any) -> tuple[str, str]:
    return (_json_path(error.absolute_path), error.message)


def _json_path(parts: Sequence[Any]) -> str:
    path = "$"
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path
