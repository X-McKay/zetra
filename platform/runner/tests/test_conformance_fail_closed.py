import copy
import hashlib
import io
import json
import tarfile

import pytest

from runner.conformance_fail_closed import evaluate, read_evidence

N = 1_000_000_000
MARKER = "a" * 16


def evidence():
    requests, logs = [], []
    for i in range(300):
        started = (100 + i / 5) * N
        status = 403 if 110 <= started / N < 133 else 200
        trace = MARKER + f"{i + 1:016x}"
        requests.append(
            dict(
                sequence=i + 1,
                trace_id=trace,
                started_ns=int(started),
                finished_ns=int(started + 0.05 * N),
                offset_ns=i * 200_000_000,
                status=status,
                error=None,
            )
        )
        logs.append(
            {
                "trace.id": trace,
                "http.status": str(status),
                "http.path": "/kubernetes/mcp",
                "http.method": "POST",
            }
        )
    observations = []
    for second in range(95, 162):
        zero = 112 <= second < 132
        count = 0 if 110 <= second < 132 else 2
        pods = (
            []
            if zero
            else [
                dict(
                    uid=f"{'new' if second >= 132 else 'old'}{i}",
                    ready=True,
                    image_id="sha256:image",
                )
                for i in (1, 2)
            ]
        )
        observations.append(
            dict(
                started_ns=second * N,
                observed_ns=int((second + 0.01) * N),
                uid="deployment",
                resource_version="1" if second < 110 else "2" if second < 132 else "3",
                generation=1 if second < 110 else 2 if second < 132 else 3,
                observed_generation=1 if second < 110 else 2 if second < 132 else 3,
                replicas=count,
                ready_replicas=len(pods),
                spec_sha="a" * 64,
                pods=pods,
                endpoints=[dict(uid=p["uid"], ready=True) for p in pods],
            )
        )
    return dict(
        probe=dict(
            schema="t17-v1",
            marker=MARKER,
            rate=5,
            duration_seconds=60,
            origin_ns=100 * N,
            token_expires_ns=180 * N,
            requests=requests,
        ),
        marker=MARKER,
        duration=60,
        receipt=dict(
            schema="t17-v1",
            marker=MARKER,
            deployment_uid="deployment",
            original_replicas=2,
            down=dict(
                started_ns=110 * N,
                acknowledged_ns=int(110.1 * N),
                resource_version="2",
                generation=2,
            ),
            up=dict(
                started_ns=132 * N,
                acknowledged_ns=int(132.1 * N),
                resource_version="3",
                generation=3,
            ),
        ),
        observations=observations,
        gateway=logs,
        logs_readable=True,
        session=dict(
            outcome="COMPLETED",
            generation=1,
            cleanup={"stop": "OK", "delete": "DELETED", "grant": "OK"},
        ),
    )


def test_complete_zero_outage_and_recovery_pass():
    assert evaluate(**evidence())["result"] == "pass"


@pytest.mark.parametrize(
    "change",
    [
        "missing_request",
        "duplicate_request",
        "gap",
        "timeout",
        "late_allow",
        "missing_log",
        "extra_log",
        "wrong_log_status",
        "wrong_path",
        "no_zero",
        "not_ready_endpoint",
        "no_recovery",
        "same_pod",
        "wrong_uid",
        "wrong_receipt_marker",
        "receipt_no_rv",
        "bad_time",
        "short_outage",
        "short_token",
        "unreadable_logs",
        "cleanup",
        "clock_jump",
        "changed_config",
        "missing_samples",
    ],
)
def test_missing_or_contradictory_evidence_never_passes(change):
    data = evidence()
    if change == "missing_request":
        data["probe"]["requests"].pop()
    elif change == "duplicate_request":
        data["probe"]["requests"][1] = copy.deepcopy(data["probe"]["requests"][0])
    elif change == "gap":
        data["probe"]["requests"][20]["offset_ns"] += 400_000_000
    elif change == "timeout":
        data["probe"]["requests"][70].update(status=None, error="ReadTimeout")
    elif change == "late_allow":
        data["probe"]["requests"][55]["status"] = 200
        data["gateway"][55]["http.status"] = "200"
    elif change == "missing_log":
        data["gateway"].pop()
    elif change == "extra_log":
        data["gateway"].append(copy.deepcopy(data["gateway"][0]))
    elif change == "wrong_log_status":
        data["gateway"][80]["http.status"] = "200"
    elif change == "wrong_path":
        data["gateway"][80]["http.path"] = "/other"
    elif change == "no_zero":
        for o in data["observations"]:
            o["pods"] = [dict(uid="old1", ready=False, image_id="sha256:image")]
    elif change == "not_ready_endpoint":
        for o in data["observations"]:
            if not o["endpoints"]:
                o["endpoints"] = [dict(uid="old1", ready=False)]
    elif change == "no_recovery":
        data["observations"] = data["observations"][:37]
    elif change == "same_pod":
        for o in data["observations"]:
            for p in o["pods"]:
                p["uid"] = p["uid"].replace("new", "old")
            for p in o["endpoints"]:
                p["uid"] = p["uid"].replace("new", "old")
    elif change == "wrong_uid":
        data["observations"][-1]["uid"] = "replacement"
    elif change == "wrong_receipt_marker":
        data["receipt"]["marker"] = "b" * 16
    elif change == "receipt_no_rv":
        data["receipt"]["down"].pop("resource_version")
    elif change == "bad_time":
        data["receipt"]["down"]["started_ns"] = 133 * N
    elif change == "short_outage":
        data["receipt"]["up"]["started_ns"] = 115 * N
    elif change == "short_token":
        data["probe"]["token_expires_ns"] = 140 * N
    elif change == "unreadable_logs":
        data["logs_readable"] = False
    elif change == "cleanup":
        data["session"]["cleanup"]["delete"] = "NOT_CONFIRMED"
    elif change == "clock_jump":
        data["probe"]["requests"][20]["started_ns"] += N
    elif change == "changed_config":
        data["observations"][-1]["spec_sha"] = "b" * 64
    elif change == "missing_samples":
        data["observations"] = [
            data["observations"][0],
            data["observations"][20],
            data["observations"][-1],
        ]
    assert evaluate(**data)["result"] == "fail"


def test_snapshot_reader_checks_hash_and_exact_regular_member(tmp_path):
    stream = io.BytesIO()
    raw = json.dumps(evidence()["probe"]).encode()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo("workspace/t17/requests.json")
        member.size = len(raw)
        archive.addfile(member, io.BytesIO(raw))
    blob = stream.getvalue()
    digest = hashlib.sha256(blob).hexdigest()
    path = tmp_path / digest[:2] / digest
    path.parent.mkdir()
    path.write_bytes(blob)
    assert read_evidence(tmp_path, digest)["marker"] == MARKER
    path.write_bytes(blob + b"tampered")
    with pytest.raises(ValueError):
        read_evidence(tmp_path, digest)


@pytest.mark.parametrize(
    "change", ["unexpected_scale", "unconfirmed_down_generation", "receipt_unknown_field"]
)
def test_unrecorded_operator_state_never_passes(change):
    data = evidence()
    if change == "unexpected_scale":
        data["observations"][16]["replicas"] = 3
    elif change == "unconfirmed_down_generation":
        data["observations"][16]["generation"] = 7
    else:
        data["receipt"]["surprise"] = "not-reviewed"
    assert evaluate(**data)["result"] == "fail"
