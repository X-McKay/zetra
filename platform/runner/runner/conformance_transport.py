"""T27 verdicts over bounded committed fixture data and host capture receipts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import tarfile
from datetime import datetime
from pathlib import Path

from runner.conformance_transport_control import capture_started


def read_probe(root, snapshot):
    if not isinstance(snapshot, str) or not re.fullmatch(r"[a-f0-9]{64}", snapshot):
        raise ValueError("transport checkpoint hash missing")
    path = Path(root) / snapshot[:2] / snapshot
    with path.open("rb") as source:
        digest, size = hashlib.sha256(), 0
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > 64 << 20:
                raise ValueError("transport checkpoint exceeds bound")
            digest.update(chunk)
        if digest.hexdigest() != snapshot:
            raise ValueError("transport checkpoint hash differs from trusted history")
        source.seek(0)
        with tarfile.open(fileobj=source, mode="r:") as archive:
            members = [m for m in archive.getmembers() if m.name == "workspace/transport.json"]
            if len(members) != 1 or not members[0].isfile() or not 0 < members[0].size <= 4096:
                raise ValueError("transport result is not one bounded regular file")
            return json.load(archive.extractfile(members[0]))


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def evaluate(data):
    probe, t4 = data.get("probe", {}), data.get("t4", {})
    positive, plain, foreign = (probe.get(k, {}) for k in ("positive", "plaintext", "foreign_ca"))
    gate = data.get("gate", {})
    start, end = data.get("capture_start"), data.get("capture_complete", {})
    # Runtime records None when the operator capture never completed. Preserve
    # that raw evidence, but evaluate missing/malformed receipts as failed checks.
    end = end if isinstance(end, dict) else {}
    analysis, stats = end.get("analysis", {}), end.get("stats", {})
    analysis = analysis if isinstance(analysis, dict) else {}
    stats = stats if isinstance(stats, dict) else {}
    session = t4.get("evidence", {}).get("session", {})
    try:
        bound = capture_started(start, gate)
    except (TypeError, ValueError):
        bound = False
    issued = t4.get("evidence", {}).get("token_issued", [])
    issuance = None
    if len(issued) == 1:
        try:
            when = datetime.fromisoformat(issued[0]["time"].replace("Z", "+00:00"))
            issuance = when.timestamp() if when.tzinfo is not None else None
        except (KeyError, TypeError, ValueError):
            pass
    times = [
        start.get("started_at") if isinstance(start, dict) else None,
        analysis.get("first_packet_at"),
        issuance,
        analysis.get("last_packet_at"),
        end.get("ended_at"),
    ]
    phases = data.get("gate_times", {})
    phase_times = [phases.get("before_exec"), times[0], phases.get("after_exec"), times[-1]]
    checks = {
        "capture_covers_all_real_t4_execs": all(finite(t) for t in phase_times)
        and phase_times == sorted(phase_times),
        "signed_transport_session_completed": data.get("transport_completed") is True,
        "verified_tls_positive_control": type(positive.get("status")) is int
        and 100 <= positive["status"] <= 599
        and positive.get("tls_url") is True
        and positive.get("credential_free") is True,
        "plaintext_refused_by_reachable_tls_listener": plain.get("result")
        in {"closed_without_http", "connection_reset", "explicit_tls_required", "tls_fatal_alert"}
        and plain.get("credential_free") is True,
        "foreign_ca_positive_and_sdk_rejection": foreign.get("positive_status") == 200
        and foreign.get("positive_requests") == 1
        and foreign.get("negative_requests") == 0
        and foreign.get("verify_code") in (20, 21)
        and foreign.get("rejected") is True
        and foreign.get("credential_free") is True,
        "exact_t4_passed": t4.get("result") == "pass"
        and bool(t4.get("checks"))
        and all(v is True for v in t4.get("checks", {}).values())
        and session.get("outcome") == "COMPLETED",
        "same_captured_generation_and_session": session.get("generation") == 1
        and bool(gate)
        and session.get("pod") == gate.get("pod")
        and session.get("session_id") == gate.get("session_id"),
        "capture_bound_to_exact_gate": bound and end.get("capture_start") == start,
        "capture_covers_exact_issuance": all(finite(t) for t in times) and times == sorted(times),
        "capture_complete_tls_streams": analysis.get("complete") is True
        and type(analysis.get("tls_connections")) is int
        and analysis["tls_connections"] > 0
        and analysis.get("tls_connections") == analysis.get("connections"),
        "no_bearer_in_cleartext": analysis.get("clear_credentials") is False,
        "capture_had_no_packet_loss": type(analysis.get("packets")) is int
        and analysis["packets"] > 0
        and stats.get("captured") == stats.get("received") == analysis["packets"]
        and stats.get("dropped") == 0,
        "capture_stayed_within_bound_and_identity": end.get("limits_not_hit") is True
        and end.get("identity_verified_through_stop") is True
        and end.get("returncode") == 0
        and end.get("file_mode") == 0o600
        and end.get("stop_reason") in {"operator_stop", "pod_deleted"},
        "capture_digest_retained": bool(
            re.fullmatch(r"[a-f0-9]{64}", str(analysis.get("sha256", "")))
        ),
        "same_worker_image_uid_and_restarts": data.get("worker_stable") is True,
        "confirmed_cleanup": data.get("cleanup_confirmed") is True,
        "all_evidence_readable": data.get("evidence_errors") == [],
    }
    return {
        "id": "T27",
        "result": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "evidence": data,
        "scope": "Fixed gateway and signed foreign-CA probe; exact sandbox-veth capture during ordinary conformance T4. Raw pcap remains private on the operator host.",
    }
