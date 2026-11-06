"""Exercise the real Kubernetes HTTP serializer against atomic Pod patch semantics."""

from __future__ import annotations

import copy
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from pathlib import Path

import pytest
from kubernetes import client

from netpol_attestor import main as attestor
from netpol_attestor.parse import MatchConfig

CFG = MatchConfig()
GOOD_CHAIN = "KUBE-POD-FW-4UFKVO2M3QBH7CNO"
TABLE = (Path(__file__).parent / "fixtures/kube-router-filter.txt").read_text()

UID = "c8e74db5-f65a-428c-a2e3-60572e4cb68b"
REPLACEMENT = "e29c645f-6bea-44bf-ad35-69a9c407467c"


def records(caplog):
    return [json.loads(r.message) for r in caplog.records if r.message.startswith("{")]


@pytest.fixture
def api(caplog):
    caplog.set_level(logging.INFO, logger="netpol-attestor")
    state = SimpleNamespace(
        pod={
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "sandbox",
                "namespace": "tenant-test",
                "uid": UID,
                "resourceVersion": "123",
                "annotations": {"unrelated": "preserve-me"},
            },
            "status": {"phase": "Running", "podIP": "10.42.1.23"},
        },
        calls=[],
        response_change=lambda p: None,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PATCH(self):
            assert self.path == "/api/v1/namespaces/tenant-test/pods/sandbox"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.calls.append((self.headers["Content-Type"], body, copy.deepcopy(records(caplog))))
            candidate = copy.deepcopy(state.pod)
            status = 200
            if isinstance(body, list):
                # Atomic JSON Patch: failed tests leave the current object untouched.
                for op in body:
                    parts = [
                        p.replace("~1", "/").replace("~0", "~") for p in op["path"].split("/")[1:]
                    ]
                    parent = candidate
                    for part in parts[:-1]:
                        parent = parent[part]
                    key = parts[-1]
                    if op["op"] == "test":
                        if parent.get(key) != op["value"]:
                            status = 422
                            break
                    elif op["op"] == "add":
                        parent[key] = op["value"]
                    elif op["op"] == "remove":
                        del parent[key]
                    else:
                        raise AssertionError("unexpected patch operation")
            else:
                # The vulnerable name-only merge patch updates a replacement.
                candidate["metadata"].setdefault("annotations", {}).update(
                    body["metadata"]["annotations"]
                )
            if status == 200:
                candidate["metadata"]["resourceVersion"] = "124"
                state.pod = candidate
                response = copy.deepcopy(candidate)
                state.response_change(response)
            else:
                response = {
                    "kind": "Status",
                    "status": "Failure",
                    "message": "sensitive response must not be logged",
                    "code": status,
                }
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = client.Configuration()
    cfg.host = f"http://127.0.0.1:{server.server_port}"
    cfg.proxy = None
    api_client = client.ApiClient(cfg)
    core = client.CoreV1Api(api_client)
    # The list result is a snapshot; PATCH always reaches the current object.
    listed = api_client._ApiClient__deserialize(copy.deepcopy(state.pod), "V1Pod")
    core.list_pod_for_all_namespaces = lambda **kw: SimpleNamespace(items=[listed])
    try:
        yield core, state, listed
    finally:
        api_client.close()
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert "sensitive response" not in caplog.text


@pytest.mark.parametrize("changed", ["uid", "resourceVersion"])
def test_replacement_or_resource_version_change_never_receives_stale_attestation(
    api, monkeypatch, caplog, changed
):
    core, state, _ = api

    def table(_):
        state.pod["metadata"][changed] = REPLACEMENT if changed == "uid" else "999"
        return TABLE

    monkeypatch.setattr(attestor, "read_filter_table", table)
    attestor.run_pass(core, "node-1", "iptables-save", CFG, 30)
    assert attestor.ANNOTATION not in state.pod["metadata"]["annotations"]
    assert len(state.calls) == 1  # never retry the old verdict against a new RV
    assert [r["event"] for r in records(caplog)] == ["chain_verified", "annotation_patch_failed"]


@pytest.mark.parametrize("annotations", [None, {"unrelated": "preserve-me"}])
def test_transport_and_receipts_bind_verification_to_exact_patch(
    api, monkeypatch, caplog, annotations
):
    core, state, listed = api
    listed.metadata.annotations = annotations
    state.pod["metadata"].pop("annotations")
    if annotations is not None:
        state.pod["metadata"]["annotations"] = annotations
    monkeypatch.setattr(attestor, "read_filter_table", lambda _: TABLE)
    attestor.run_pass(core, "node-1", "iptables-save", CFG, 30)
    content_type, patch, before_call = state.calls[0]
    assert content_type == "application/json-patch+json"
    assert patch[:2] == [
        {"op": "test", "path": "/metadata/uid", "value": UID},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "123"},
    ]
    before, after = records(caplog)
    assert before_call == [before]  # chain evidence was emitted before real HTTP PATCH
    assert (before["event"], after["event"]) == ("chain_verified", "annotation_patch_ack")
    assert before["operation_id"] == after["operation_id"]
    assert before["process_id"] == after["process_id"]
    assert before["sequence"] < after["sequence"]
    assert before["monotonic_ns"] <= after["monotonic_ns"]
    assert before["pod_uid"] == after["pod_uid"] == UID
    assert before["source_resource_version"] == after["source_resource_version"] == "123"
    assert after["resource_version"] == "124"
    assert (
        before["annotation"]
        == after["annotation"]
        == state.pod["metadata"]["annotations"][attestor.ANNOTATION]
    )
    assert before["chain"] == GOOD_CHAIN
    if annotations is not None:
        assert state.pod["metadata"]["annotations"]["unrelated"] == "preserve-me"


@pytest.mark.parametrize("field", ["uid", "resource_version"])
def test_missing_immutable_patch_binding_fails_closed(api, monkeypatch, caplog, field):
    core, state, listed = api
    setattr(listed.metadata, field, None)
    monkeypatch.setattr(attestor, "read_filter_table", lambda _: TABLE)
    attestor.run_pass(core, "node-1", "iptables-save", CFG, 30)
    assert state.calls == []
    assert "chain_verified" not in caplog.text


def test_mismatched_response_is_never_an_ack(api, monkeypatch, caplog):
    core, state, _ = api
    state.response_change = lambda p: p["metadata"].update(uid=REPLACEMENT)
    monkeypatch.setattr(attestor, "read_filter_table", lambda _: TABLE)
    attestor.run_pass(core, "node-1", "iptables-save", CFG, 30)
    assert [r["event"] for r in records(caplog)] == ["chain_verified", "annotation_patch_failed"]


def test_rejected_chain_emits_safe_reason_without_patch(api, monkeypatch, caplog):
    core, state, _ = api
    monkeypatch.setattr(attestor, "read_filter_table", lambda _: "*filter\nCOMMIT\n")
    attestor.run_pass(core, "node-1", "iptables-save", CFG, 30)
    assert not state.calls
    assert [(r["event"], r["reason"], r["pod_uid"]) for r in records(caplog)] == [
        ("chain_not_verified", "no_chain", UID)
    ]


@pytest.mark.parametrize("withdraw", [False, True])
def test_refresh_and_withdrawal_preserve_unrelated_annotations(api, monkeypatch, caplog, withdraw):
    core, state, listed = api
    old = "2020-01-01T00:00:00Z;" + GOOD_CHAIN
    listed.metadata.annotations[attestor.ANNOTATION] = old
    state.pod["metadata"]["annotations"][attestor.ANNOTATION] = old
    monkeypatch.setattr(
        attestor, "read_filter_table", lambda _: "*filter\nCOMMIT\n" if withdraw else TABLE
    )
    attestor.run_pass(core, "node-1", "iptables-save", CFG, 30)
    annotations = state.pod["metadata"]["annotations"]
    assert annotations["unrelated"] == "preserve-me"
    assert (attestor.ANNOTATION not in annotations) is withdraw
    if not withdraw:
        assert annotations[attestor.ANNOTATION] != old
    assert [r["event"] for r in records(caplog)] == [
        "attestation_withdrawal" if withdraw else "chain_verified",
        "annotation_patch_ack",
    ]


def test_stale_withdrawal_cannot_remove_replacement_annotation(api, monkeypatch, caplog):
    core, state, listed = api
    listed.metadata.annotations[attestor.ANNOTATION] = "2020-01-01T00:00:00Z;" + GOOD_CHAIN
    replacement_value = "2026-11-06T12:00:00Z;" + GOOD_CHAIN

    def table(_):
        state.pod["metadata"].update(uid=REPLACEMENT, resourceVersion="999")
        state.pod["metadata"]["annotations"][attestor.ANNOTATION] = replacement_value
        return "*filter\nCOMMIT\n"

    monkeypatch.setattr(attestor, "read_filter_table", table)
    attestor.run_pass(core, "node-1", "iptables-save", CFG, 30)
    assert state.pod["metadata"]["annotations"][attestor.ANNOTATION] == replacement_value
    assert records(caplog)[-1]["event"] == "annotation_patch_failed"


def test_old_untrusted_annotation_is_never_logged(api, monkeypatch, caplog):
    core, state, listed = api
    secret = "Bearer do-not-export-this-test-credential"
    listed.metadata.annotations[attestor.ANNOTATION] = secret
    state.pod["metadata"]["annotations"][attestor.ANNOTATION] = secret
    monkeypatch.setattr(attestor, "read_filter_table", lambda _: "*filter\nCOMMIT\n")
    attestor.run_pass(core, "node-1", "iptables-save", CFG, 30)
    assert secret not in caplog.text
    assert records(caplog)[-1]["event"] == "annotation_patch_ack"
