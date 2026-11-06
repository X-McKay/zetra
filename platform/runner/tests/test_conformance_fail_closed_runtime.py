import asyncio
import copy
import json
from types import SimpleNamespace as NS

import pytest

from runner.conformance_fail_closed_runtime import OutageOps, FailClosedHarness, receipt_progress


def test_receipt_is_append_only_and_pins_prepared_identity():
    before = {
        "schema": "t17-v1",
        "marker": "a" * 16,
        "deployment_uid": "uid",
        "original_replicas": 2,
    }
    after = {
        **before,
        "down": {"started_ns": 1, "acknowledged_ns": 2, "resource_version": "3", "generation": 2},
    }
    assert receipt_progress(before, after) == after
    for key, value in [("marker", "b" * 16), ("deployment_uid", "new"), ("original_replicas", 3)]:
        changed = {**after, key: value}
        with pytest.raises(ValueError):
            receipt_progress(before, changed)
    changed = copy.deepcopy(after)
    changed["down"]["started_ns"] = 5
    with pytest.raises(ValueError):
        receipt_progress(after, changed)
    with pytest.raises(ValueError):
        receipt_progress(after, before)


def test_control_configmap_is_bounded_and_uid_pinned():
    config = NS(
        metadata=NS(uid="original"), data={"receipt.json": json.dumps({"marker": "a" * 16})}
    )
    ops = OutageOps(NS(read_namespaced_config_map=lambda *a, **k: config), None, None)
    assert ops.receipt()["marker"] == "a" * 16
    config.metadata.uid = "replacement"
    with pytest.raises(ValueError):
        ops.receipt()


@pytest.mark.asyncio
async def test_cancellation_waits_for_cleanup_and_cancels_monitor():
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []
    harness = object.__new__(FailClosedHarness)

    async def close(*args):
        entered.set()
        await release.wait()
        closed.append(True)

    harness.cleanup = NS(_close=close)
    result = asyncio.create_task(asyncio.sleep(10))
    monitor = asyncio.create_task(asyncio.sleep(10))
    task = asyncio.create_task(harness.close(NS(), NS(), result, monitor))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not monitor.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True] and monitor.done()
    result.cancel()
    await asyncio.gather(result, return_exceptions=True)


def test_cli_t17_requires_explicit_fixture_marker_and_receipt_opt_in():
    from runner.conformance import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--release", "main", "--tests", "T17"])
    args = parse_args(
        [
            "--release",
            "main",
            "--tests",
            "T17",
            "--fail-closed-fixture-release",
            "signed-b",
            "--aps-outage-marker",
            "a" * 16,
            "--observe-aps-outage",
        ]
    )
    assert args.tests == ["T17"] and args.aps_outage_duration == 180
