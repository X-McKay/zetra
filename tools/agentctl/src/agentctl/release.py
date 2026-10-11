from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentctl.diagnostics import Diagnostic, emit_diagnostics
from agentctl.files import DataFileError, load_data


def check_release(
    *,
    report_path: Path,
    policy_path: Path,
    baseline_path: Path | None = None,
    output_format: str = "text",
) -> int:
    report_path = report_path.resolve()
    policy_path = policy_path.resolve()
    try:
        report = load_data(report_path)
        policy = load_data(policy_path)
        baseline = load_data(baseline_path.resolve()) if baseline_path else None
    except DataFileError as exc:
        raise ValueError(str(exc)) from exc

    diagnostics: list[Diagnostic] = []
    report_subject = _report_subject(report)
    if report_subject is None:
        diagnostics.append(
            Diagnostic(
                "RELEASE025",
                "report must identify an agent or system subject",
                str(report_path),
            )
        )
    if report.get("schema_version") != policy.get("schema_version", 1):
        diagnostics.append(
            Diagnostic(
                "RELEASE001",
                "report and policy schema versions do not match",
                str(report_path),
            )
        )
    if baseline is not None:
        baseline_subject = _report_subject(baseline)
        if baseline_subject is None:
            diagnostics.append(
                Diagnostic(
                    "RELEASE025",
                    "baseline must identify an agent or system subject",
                    str(baseline_path),
                )
            )
        elif report_subject is not None and report_subject != baseline_subject:
            diagnostics.append(
                Diagnostic(
                    "RELEASE021",
                    "current report and baseline refer to different subjects",
                    str(report_path),
                )
            )
    gates = report.get("hard_gates")
    metrics = report.get("metrics")
    if not isinstance(gates, Mapping):
        diagnostics.append(
            Diagnostic("RELEASE002", "hard_gates must be a mapping", str(report_path))
        )
        gates = {}
    if not isinstance(metrics, Mapping):
        diagnostics.append(
            Diagnostic("RELEASE003", "metrics must be a mapping", str(report_path))
        )
        metrics = {}

    expected_gates = policy.get("hard_gates", {})
    if not isinstance(expected_gates, Mapping):
        diagnostics.append(
            Diagnostic(
                "RELEASE004", "policy hard_gates must be a mapping", str(policy_path)
            )
        )
    else:
        for name, expected in expected_gates.items():
            if name not in gates:
                diagnostics.append(
                    Diagnostic(
                        "RELEASE005", f"missing hard gate {name!r}", str(report_path)
                    )
                )
                continue
            observed = gates[name]
            if not _gate_values_equal(observed, expected):
                diagnostics.append(
                    Diagnostic(
                        "RELEASE006",
                        f"hard gate {name!r} expected {expected!r}, observed {observed!r}",
                        str(report_path),
                    )
                )

    thresholds = policy.get("thresholds", {})
    if not isinstance(thresholds, Mapping):
        diagnostics.append(
            Diagnostic(
                "RELEASE007", "policy thresholds must be a mapping", str(policy_path)
            )
        )
    else:
        diagnostics.extend(_check_thresholds(metrics, thresholds, report_path))

    required_provenance = policy.get("required_provenance", [])
    if not isinstance(required_provenance, list) or not all(
        isinstance(item, str) and item for item in required_provenance
    ):
        diagnostics.append(
            Diagnostic(
                "RELEASE022",
                "policy required_provenance must be a list of field names",
                str(policy_path),
            )
        )
    else:
        provenance = report.get("provenance")
        if required_provenance and not isinstance(provenance, Mapping):
            diagnostics.append(
                Diagnostic(
                    "RELEASE023",
                    "report provenance must be a mapping",
                    str(report_path),
                )
            )
        elif isinstance(provenance, Mapping):
            for field in required_provenance:
                if field not in provenance or provenance[field] in (None, "", [], {}):
                    diagnostics.append(
                        Diagnostic(
                            "RELEASE024",
                            f"missing required provenance field {field!r}",
                            str(report_path),
                        )
                    )

    regressions = policy.get("regressions", {})
    if baseline_path and baseline is not None:
        baseline_metrics = baseline.get("metrics")
        if not isinstance(baseline_metrics, Mapping):
            diagnostics.append(
                Diagnostic(
                    "RELEASE008",
                    "baseline metrics must be a mapping",
                    str(baseline_path),
                )
            )
        elif not isinstance(regressions, Mapping):
            diagnostics.append(
                Diagnostic(
                    "RELEASE009",
                    "policy regressions must be a mapping",
                    str(policy_path),
                )
            )
        else:
            diagnostics.extend(
                _check_regressions(metrics, baseline_metrics, regressions, report_path)
            )
    elif regressions and baseline_path is None:
        diagnostics.append(
            Diagnostic(
                "RELEASE010",
                "regression policy requires an approved baseline",
                str(policy_path),
            )
        )

    return emit_diagnostics(
        diagnostics, output_format=output_format, heading="Release gate evaluation"
    )


def _check_thresholds(
    metrics: Mapping[str, Any], thresholds: Mapping[str, Any], report_path: Path
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for name, rule in thresholds.items():
        if name not in metrics:
            diagnostics.append(
                Diagnostic("RELEASE011", f"missing metric {name!r}", str(report_path))
            )
            continue
        observed = metrics[name]
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            diagnostics.append(
                Diagnostic(
                    "RELEASE012", f"metric {name!r} must be numeric", str(report_path)
                )
            )
            continue
        if (
            not isinstance(rule, Mapping)
            or not rule
            or not set(rule).issubset({"min", "max"})
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in rule.values()
            )
        ):
            diagnostics.append(
                Diagnostic(
                    "RELEASE013",
                    f"threshold {name!r} must contain min and/or max",
                    str(report_path),
                )
            )
            continue
        if "min" in rule and observed < rule["min"]:
            diagnostics.append(
                Diagnostic(
                    "RELEASE014",
                    f"metric {name!r} minimum is {rule['min']}, observed {observed}",
                    str(report_path),
                )
            )
        if "max" in rule and observed > rule["max"]:
            diagnostics.append(
                Diagnostic(
                    "RELEASE015",
                    f"metric {name!r} maximum is {rule['max']}, observed {observed}",
                    str(report_path),
                )
            )
    return diagnostics


def _gate_values_equal(observed: Any, expected: Any) -> bool:
    if isinstance(observed, bool) or isinstance(expected, bool):
        return type(observed) is type(expected) and observed == expected
    if isinstance(observed, (int, float)) and isinstance(expected, (int, float)):
        return float(observed) == float(expected)
    return type(observed) is type(expected) and observed == expected


def _report_subject(payload: Mapping[str, Any]) -> tuple[str, str] | None:
    subject = payload.get("subject")
    if isinstance(subject, Mapping):
        kind = subject.get("kind")
        name = subject.get("name")
        if kind in {"agent", "system"} and isinstance(name, str) and name.strip():
            return kind, name
        return None
    agent = payload.get("agent")
    if isinstance(agent, str) and agent.strip():
        return "agent", agent
    return None


def _check_regressions(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any],
    rules: Mapping[str, Any],
    report_path: Path,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for name, rule in rules.items():
        if name not in current or name not in baseline:
            diagnostics.append(
                Diagnostic(
                    "RELEASE016",
                    f"metric {name!r} is missing from current or baseline",
                    str(report_path),
                )
            )
            continue
        if not isinstance(rule, Mapping):
            diagnostics.append(
                Diagnostic(
                    "RELEASE017",
                    f"regression rule {name!r} must be a mapping",
                    str(report_path),
                )
            )
            continue
        direction = rule.get("direction")
        delta = rule.get("max_delta")
        observed = current[name]
        previous = baseline[name]
        if (
            direction not in {"higher", "lower"}
            or isinstance(delta, bool)
            or not isinstance(delta, (int, float))
            or delta < 0
        ):
            diagnostics.append(
                Diagnostic(
                    "RELEASE018",
                    f"regression rule {name!r} is invalid",
                    str(report_path),
                )
            )
            continue
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in (observed, previous)
        ):
            diagnostics.append(
                Diagnostic(
                    "RELEASE019",
                    f"regression metric {name!r} must be numeric",
                    str(report_path),
                )
            )
            continue
        failed = (
            observed < previous - delta
            if direction == "higher"
            else observed > previous + delta
        )
        if failed:
            diagnostics.append(
                Diagnostic(
                    "RELEASE020",
                    f"metric {name!r} regressed: baseline {previous}, observed {observed}, allowed delta {delta}",
                    str(report_path),
                )
            )
    return diagnostics
