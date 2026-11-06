import pytest

from runner.conformance_transport import evaluate
from tests.test_transport_control import gate, receipt


def evidence():
    start = receipt()
    at = start["started_at"]
    t4 = {
        "result": "pass",
        "checks": {"exact_claims": True, "one_token_per_sandbox": True},
        "evidence": {
            "session": {
                "session_id": gate()["session_id"],
                "pod": gate()["pod"],
                "generation": 1,
                "outcome": "COMPLETED",
            },
            "token_issued": [{"time": "2026-11-06T18:33:21Z"}],
        },
    }
    return {
        "probe": {
            "version": 1,
            "positive": {"status": 400, "credential_free": True, "tls_url": True},
            "plaintext": {"result": "closed_without_http", "credential_free": True},
            "foreign_ca": {
                "positive_status": 200,
                "positive_requests": 1,
                "negative_requests": 0,
                "verify_code": 20,
                "rejected": True,
                "credential_free": True,
            },
        },
        "transport_completed": True,
        "t4": t4,
        "gate": gate(),
        "gate_times": {"before_exec": at - 1, "after_exec": at + 3},
        "capture_start": start,
        "capture_complete": {
            "capture_start": start,
            "ended_at": at + 5,
            "stop_reason": "operator_stop",
            "returncode": 0,
            "stats": {"captured": 10, "received": 10, "dropped": 0},
            "file_mode": 0o600,
            "limits_not_hit": True,
            "identity_verified_through_stop": True,
            "analysis": {
                "complete": True,
                "clear_credentials": False,
                "tls_connections": 1,
                "connections": 1,
                "packets": 10,
                "bytes": 3000,
                "sha256": "a" * 64,
                "first_packet_at": at + 0.1,
                "last_packet_at": at + 4,
            },
        },
        "worker_stable": True,
        "cleanup_confirmed": True,
        "evidence_errors": [],
    }


def test_t27_requires_complete_real_transport_capture_and_exact_t4():
    assert evaluate(evidence())["result"] == "pass"


@pytest.mark.parametrize(
    "path,value",
    [
        (("probe", "plaintext", "result"), "error"),
        (("probe", "foreign_ca", "verify_code"), 62),
        (("probe", "foreign_ca", "negative_requests"), 1),
        (("probe", "positive", "credential_free"), False),
        (("t4", "checks", "one_token_per_sandbox"), False),
        (("t4", "evidence", "session", "generation"), 2),
        (("capture_complete", "analysis", "complete"), False),
        (("capture_complete", "analysis", "clear_credentials"), True),
        (("capture_complete", "stats", "dropped"), 1),
        (("capture_complete", "stats", "received"), 11),
        (("capture_complete", "limits_not_hit"), False),
        (("capture_complete", "identity_verified_through_stop"), False),
        (("cleanup_confirmed",), False),
    ],
)
def test_missing_or_failed_evidence_never_qualifies(path, value):
    data = evidence()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert evaluate(data)["result"] == "fail"


def test_issuance_outside_capture_does_not_establish_transport():
    data = evidence()
    data["t4"]["evidence"]["token_issued"][0]["time"] = "2026-11-06T17:00:00Z"
    assert evaluate(data)["result"] == "fail"


def test_probe_reader_uses_actual_artifact_store_and_committed_digest(tmp_path):
    import io
    import json
    import tarfile
    from runner.activities import FilesystemStore
    from runner.conformance_transport import read_probe

    value = {"version": 1}
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w") as archive:
        raw = json.dumps(value).encode()
        info = tarfile.TarInfo("workspace/transport.json")
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    source.seek(0)
    import hashlib

    key = hashlib.sha256(source.getvalue()).hexdigest()
    FilesystemStore(tmp_path).put(key, source)
    assert read_probe(tmp_path, key) == value


def test_capture_must_continue_until_real_final_exec_gate():
    data = evidence()
    data["gate_times"] = {
        "before_exec": data["capture_start"]["started_at"] - 1,
        "after_exec": data["capture_complete"]["ended_at"] + 1,
    }
    assert evaluate(data)["result"] == "fail"


@pytest.mark.parametrize("missing", [None, {}, [], "missing"])
def test_missing_capture_completion_reports_failed_checks(missing):
    data = evidence()
    data["capture_complete"] = missing
    report = evaluate(data)
    assert report["result"] == "fail"
    assert report["evidence"]["capture_complete"] == missing
    for name in (
        "capture_bound_to_exact_gate",
        "capture_covers_exact_issuance",
        "capture_complete_tls_streams",
        "no_bearer_in_cleartext",
        "capture_had_no_packet_loss",
        "capture_stayed_within_bound_and_identity",
        "capture_digest_retained",
    ):
        assert report["checks"][name] is False


def test_strict_tls_fatal_alert_refusal_is_accepted_only_with_credentials_absent():
    data = evidence()
    data["probe"]["plaintext"]["result"] = "tls_fatal_alert"
    assert evaluate(data)["result"] == "pass"
    data["probe"]["plaintext"]["credential_free"] = False
    assert evaluate(data)["result"] == "fail"
