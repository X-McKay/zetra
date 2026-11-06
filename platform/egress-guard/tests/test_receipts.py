"""Receipts observe the existing guard predicates; they never relax them."""

import json
from pathlib import Path
from types import SimpleNamespace

from egress_guard import main as eg

KEY = "agentplatform.io/netpol-attested"
VALUE = "2026-11-06T12:00:00Z;KUBE-POD-FW-ABCDEF0123456789"


def arguments(tmp_path, value=VALUE):
    path = tmp_path / "annotations"
    path.write_text(f"{KEY}={json.dumps(value)}\n")
    return SimpleNamespace(
        gateway="127.0.0.1:2222",
        forbidden="127.0.0.1:1111,127.0.0.1:3333",
        attestation=str(path),
        attestation_key=KEY,
        timeout=0.02,
        connect_timeout=0.01,
        interval=0.001,
    )


def records(capsys):
    result = capsys.readouterr()
    return [json.loads(line) for line in result.out.splitlines() if line.startswith("{")], result


def test_observation_precedes_gateway_and_exit_links_the_exact_read(tmp_path, monkeypatch, capsys):
    args = arguments(tmp_path)
    calls = []
    real_print = print

    def emit(*a, **kw):
        if str(a[0]).startswith("{"):
            calls.append(json.loads(a[0])["event"])
        real_print(*a, **kw)

    monkeypatch.setattr("builtins.print", emit)

    def connect(host, port, timeout):
        calls.append(port)
        return port == 2222

    monkeypatch.setattr(eg, "can_connect", connect)
    assert eg.guard(args) == 0
    observed, exited = records(capsys)[0]
    assert calls == [1111, 3333, "annotation_observed", 2222, "guard_exit"]
    assert observed["annotation"] == exited["annotation"] == VALUE
    assert observed["observation_id"] == exited["observation_id"]
    assert observed["process_id"] == exited["process_id"]
    assert observed["sequence"] < exited["sequence"]
    assert observed["monotonic_ns"] <= exited["monotonic_ns"]
    assert exited["exit_code"] == 0


def test_leak_still_exits_before_any_annotation_read_or_retry(tmp_path, monkeypatch, capsys):
    args = arguments(tmp_path)
    calls = []
    monkeypatch.setattr(eg, "can_connect", lambda host, port, timeout: calls.append(port) or True)
    monkeypatch.setattr(
        eg, "read_annotations", lambda _: (_ for _ in ()).throw(AssertionError("read after leak"))
    )
    assert eg.guard(args) == 3
    rows, output = records(capsys)
    assert calls == [1111]
    assert [r["event"] for r in rows] == ["guard_exit"]
    assert rows[0]["exit_code"] == 3
    assert "LEAK 127.0.0.1:1111" in output.err


def test_timeout_is_not_reported_as_an_observed_annotation(tmp_path, monkeypatch, capsys):
    args = arguments(tmp_path, "missing")
    monkeypatch.setattr(eg, "can_connect", lambda *a: False)
    assert eg.guard(args) == 4
    rows, _ = records(capsys)
    assert [(r["event"], r["exit_code"]) for r in rows] == [("guard_exit", 4)]


def test_each_retry_binds_only_the_annotation_read_for_that_attempt(tmp_path, monkeypatch, capsys):
    args = arguments(tmp_path)
    changed = VALUE.replace("12:00:00", "12:00:30")
    gateways = 0

    def connect(host, port, timeout):
        nonlocal gateways
        if port != 2222:
            return False
        gateways += 1
        Path(args.attestation).write_text(f"{KEY}={json.dumps(changed)}\n")
        return gateways == 2

    monkeypatch.setattr(eg, "can_connect", connect)
    assert eg.guard(args) == 0
    first, second, exited = records(capsys)[0]
    assert first["annotation"] == VALUE
    assert second["annotation"] == exited["annotation"] == changed
    assert first["observation_id"] != second["observation_id"] == exited["observation_id"]


def test_unsupported_annotation_keeps_guard_behavior_without_exporting_arbitrary_text(
    tmp_path, monkeypatch, capsys
):
    secret = "Bearer do-not-export-this-test-credential"
    args = arguments(tmp_path, "2026-11-06T12:00:00Z;" + secret)
    monkeypatch.setattr(eg, "can_connect", lambda host, port, timeout: port == 2222)
    assert eg.guard(args) == 0  # Existing timestamp/nonempty-chain semantics are unchanged.
    rows, output = records(capsys)
    assert secret not in output.out + output.err
    assert len(rows) == 2
    assert all(row["annotation_recorded"] is False and row["annotation"] is None for row in rows)
