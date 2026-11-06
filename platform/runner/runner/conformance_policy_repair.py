"""T28 evaluation: repair and refusal must concern the same exact injected policy."""

from __future__ import annotations

import math


def evaluate(data: dict) -> dict:
    baseline = data.get("baseline") or {}
    extra = data.get("extra") or {}
    repair = data.get("repair") or {}
    session = data.get("session") or {}
    failure = session.get("failure") or {}
    details = failure.get("details") or {}
    cleanup = data.get("cleanup") or {}
    elapsed = repair.get("elapsed")
    expected_names = set(baseline.get("names", [])) | {extra.get("name")}
    policies = details.get("policies") or []
    checks = {
        "no_runtime_error": "error_type" not in data,
        "confirmed_policy_creation": extra.get("creation_confirmed") is True,
        "canonical_clean_baseline": baseline.get("valid") is True,
        "canonical_repair_within_60_seconds": (
            type(elapsed) in (float, int)
            and math.isfinite(elapsed)
            and 0 <= elapsed <= 60
            and repair.get("valid") is True
            and repair.get("extra_absent") is True
            and repair.get("digest") == baseline.get("digest")
        ),
        "controller_stable": bool(data.get("controller_before"))
        and data.get("controller_before") == data.get("controller_after"),
        "controller_acknowledged_exact_uid": len(
            [
                r
                for r in data.get("receipts", [])
                if isinstance(r, dict)
                and r.get("event") == "tenant_network_policy_deleted"
                and r.get("tenant") == data.get("tenant")
                and r.get("name") == extra.get("name")
                and r.get("uid") == extra.get("uid")
                and r.get("resource_version") == extra.get("resource_version")
            ]
        )
        == 1,
        "real_preflight_digest_refusal": (
            session.get("outcome") == "FAILED"
            and failure.get("activity") == "preflight"
            and failure.get("type") == "Forbidden"
            and failure.get("non_retryable") is True
            and details.get("kind") == "effective_network_policy_mismatch"
            and details.get("tenant") == data.get("tenant")
            and details.get("session_id") == data.get("session_id")
            and details.get("digest") == data.get("drift_digest")
            and details.get("digest") != baseline.get("digest")
            and details.get("published_digest") == baseline.get("digest")
            and details.get("truncated") is False
            and len(policies) == len(expected_names)
            and {p.get("name") for p in policies} == expected_names
            and {"name": extra.get("name"), "uid": extra.get("uid")} in policies
        ),
        "no_sandbox_provisioning": bool(session.get("activities"))
        and "preflight" in session["activities"]
        and not set(session["activities"]) & {"set_generation", "create_sandbox", "run_boundary"}
        and session.get("pods_absent") is True,
        "cleanup_confirmed_without_operator_repair": cleanup.get("operator_deleted") is False
        and cleanup.get("extra_absent") is True
        and cleanup.get("session_closed") is True
        and cleanup.get("session_pods_absent") is True,
    }
    return {
        "id": "T28",
        "name": "automatic effective NetworkPolicy repair",
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "evidence": data,
    }
