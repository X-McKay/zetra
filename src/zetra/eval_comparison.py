"""Data-only, fixed-plan comparisons of complete unsigned experiment evidence."""

import json
from pathlib import Path
from statistics import mean

from .eval_cases import _unique_object
from .eval_reports import validate_report
from .eval_statistics import metric_summary, wilson

MAX_REPORT_BYTES = 64 * 1024 * 1024


def read_report(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("experiment report too large")
    try:
        return validate_report(json.loads(raw, object_pairs_hook=_unique_object))
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("invalid experiment report encoding or nesting") from exc


def compare_reports(baseline, candidate):
    baseline, candidate = validate_report(baseline), validate_report(candidate)
    for field in (
        "agent",
        "agent_sha256",
        "experiment",
        "plan_sha256",
        "plan_file_sha256",
        "dataset_sha256",
        "evaluator_sha256",
        "mcp_binding",
        "mode",
    ):
        if baseline[field] != candidate[field]:
            raise ValueError(f"incompatible experiment reports: {field}")
    for report in (baseline, candidate):
        if report["counts"]["not_run"] or report["integrity_errors"] or report["stop_reason"]:
            raise ValueError("comparison requires complete, unmodified experiment evidence")
    if [c["id"] for c in baseline["cases"]] != [c["id"] for c in candidate["cases"]]:
        raise ValueError("incompatible case order or identities")
    for a, b in zip(baseline["cases"], candidate["cases"], strict=True):
        for field in ("case_sha256", "tags", "cluster", "assertion_types"):
            if a[field] != b[field]:
                raise ValueError(f"incompatible case evidence: {field}")
    plan = candidate["plan"]
    # Union bound over 2 configurations x N fixed cases. No independence between
    # baseline and candidate is needed for this conservative endpoint subtraction.
    confidence = 1 - (1 - plan["confidence"]) / (2 * len(candidate["cases"]))
    rows = []
    for a, b in zip(baseline["cases"], candidate["cases"], strict=True):
        successes_a = sum(t["outcome"] == "passed" for t in a["trials"])
        successes_b = sum(t["outcome"] == "passed" for t in b["trials"])
        interval_a = wilson(successes_a, plan["trials"], confidence)
        interval_b = wilson(successes_b, plan["trials"], confidence)
        metrics = {}
        for name in ("duration_ms", "input_tokens", "output_tokens", "steps"):
            left = metric_summary([t["metrics"][name] for t in a["trials"]])
            right = metric_summary([t["metrics"][name] for t in b["trials"]])
            metrics[name] = {
                "baseline": left,
                "candidate": right,
                "mean_delta": right["mean"] - left["mean"]
                if left["mean"] is not None
                and right["mean"] is not None
                and not left["missing"]
                and not right["missing"]
                else None,
            }
        rows.append(
            {
                "id": a["id"],
                "baseline_success": successes_a / plan["trials"],
                "candidate_success": successes_b / plan["trials"],
                "delta": (successes_b - successes_a) / plan["trials"],
                "interval": {
                    "lower": interval_b["lower"] - interval_a["upper"],
                    "upper": interval_b["upper"] - interval_a["lower"],
                },
                "metrics": metrics,
            }
        )
    if candidate["status"] == "FAILED" or any(
        r["interval"]["upper"] < -plan["max_regression"] for r in rows
    ):
        status = "FAILED"
    elif candidate["status"] != "PASSED" or any(
        r["interval"]["lower"] < -plan["max_regression"] for r in rows
    ):
        status = "INCONCLUSIVE"
    else:
        status = "PASSED"
    identity = ("source_sha256", "model_config", "model_config_sha256", "created_at")
    return {
        "schema_version": 1,
        "status": status,
        "trust": "local-unsigned",
        "scope": "fixed-case noninferiority under independent stable draws; not unseen-task accuracy or production authorization",
        "agent": candidate["agent"],
        "experiment": candidate["experiment"],
        "dataset_sha256": candidate["dataset_sha256"],
        "plan_sha256": candidate["plan_sha256"],
        "baseline": {k: baseline[k] for k in identity},
        "candidate": {k: candidate[k] for k in identity},
        "distinct_cases": len(rows),
        "case_mean_delta": mean(r["delta"] for r in rows),
        "improved": [r["id"] for r in rows if r["delta"] > 0],
        "regressed": [r["id"] for r in rows if r["delta"] < 0],
        "tied": [r["id"] for r in rows if r["delta"] == 0],
        "interval_method": "difference of Wilson endpoints, Bonferroni across configurations and cases",
        "family_confidence": plan["confidence"],
        "max_regression": plan["max_regression"],
        "cases": rows,
    }
