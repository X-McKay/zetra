import json

import pytest

from runner import memory_audit as m
from runner.types import BoundaryIn
from tests.test_activities import inp, run, setup
from runner import activities as act
from tests.fakes import FakeStream, FakeWS


def test_reads_process_identity_rss_hwm_and_actual_cgroup_limit(tmp_path):
    (tmp_path / "status").write_text("Name:\tpython\nVmRSS:\t100 kB\nVmHWM:\t120 kB\n")
    (tmp_path / "stat").write_text("17 (python worker) " + " ".join(["0"] * 19 + ["1234"]))
    limit = tmp_path / "memory.max"
    limit.write_text(str(1 << 30))
    facts = m.process_memory(tmp_path, [limit], hostname="runner-pod")
    assert facts == {
        "process_id": "runner-pod:17:1234",
        "rss_bytes": 102400,
        "hwm_bytes": 122880,
        "limit_bytes": 1 << 30,
    }
    limit.write_text("max")
    with pytest.raises(ValueError):
        m.process_memory(tmp_path, [limit], hostname="runner-pod")


def test_bounded_sampling_reports_missing_data_not_a_made_up_zero(monkeypatch):
    events = []
    now = [0.0]
    monkeypatch.setattr(m.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(m.records, "emit", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setattr(m, "process_memory", lambda: (_ for _ in ()).throw(OSError("denied")))
    sampler = m.MemoryAudit(BoundaryIn(inp(), 1, 0))
    sampler.sample("start")
    for _ in range(100):
        sampler.sample("periodic")
    now[0] = 5.0
    sampler.sample("periodic")
    sampler.sample("end")
    assert [e[1]["phase"] for e in events] == ["start", "periodic", "end"]
    assert all(e[1]["error"] == "OSError" and e[1]["rss_bytes"] is None for e in events)


def test_unknown_outcome_still_records_start_and_final_memory(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(
        m,
        "process_memory",
        lambda: {
            "process_id": "runner:17:1234",
            "rss_bytes": 100,
            "hwm_bytes": 120,
            "limit_bytes": 1024,
        },
    )
    setup(tmp_path, stream=FakeStream(FakeWS([b"not json"], rc=0)))
    with pytest.raises(Exception):
        run(act.run_boundary, BoundaryIn(inp(), 1, 0))
    samples = [
        json.loads(r.message)
        for r in caplog.records
        if r.message.startswith("{") and '"boundary_memory"' in r.message
    ]
    assert [s["phase"] for s in samples] == ["start", "end"]
    assert all(s["process_id"] for s in samples)
