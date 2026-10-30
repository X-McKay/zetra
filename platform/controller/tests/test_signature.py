from __future__ import annotations

import base64
import json
import subprocess

from registry_controller.signature import CosignVerifier, attestation_matches

IMAGE = "registry.example.com/agents/x@sha256:" + "a" * 64
M, P = "4e" * 32, "9c" * 32


def envelope(manifest, policy) -> str:
    statement = {"predicate": {"manifestSha256": manifest, "policySha256": policy}}
    return json.dumps({"payload": base64.b64encode(json.dumps(statement).encode()).decode()})


class Runner:
    def __init__(self, results):
        self.results = list(results)
        self.argv = []

    def __call__(self, argv, **kw):
        self.argv.append(argv)
        r = self.results.pop(0)
        if isinstance(r, BaseException):
            raise r
        code, out = r
        return subprocess.CompletedProcess(argv, code, out, "")


def test_homelab_verifies_signature_only():
    run = Runner([(0, "[]")])
    assert CosignVerifier("/k.pub", run=run).verify(IMAGE, M, P)
    assert run.argv == [["cosign", "verify", "--key", "/k.pub", "--output", "json", IMAGE]]


def test_fail_closed_on_refusal_error_or_tag():
    assert not CosignVerifier("/k.pub", run=Runner([(1, "")])).verify(IMAGE, M, P)
    assert not CosignVerifier("/k.pub", run=Runner([FileNotFoundError()])).verify(IMAGE, M, P)
    timeout = subprocess.TimeoutExpired(["cosign"], 60)
    assert not CosignVerifier("/k.pub", run=Runner([timeout])).verify(IMAGE, M, P)
    assert not CosignVerifier("/k.pub", run=Runner([])).verify("registry/x:latest", M, P)


def test_enterprise_requires_matching_attestation():
    ok = Runner([(0, "[]"), (0, envelope(M, P) + "\n")])
    assert CosignVerifier("/k.pub", profile="enterprise", run=ok).verify(IMAGE, M, P)
    bad = Runner([(0, "[]"), (0, envelope(M, "00" * 32))])
    assert not CosignVerifier("/k.pub", profile="enterprise", run=bad).verify(IMAGE, M, P)


def test_positive_results_are_cached_negative_are_not():
    run = Runner([(1, ""), (0, "[]")])
    v = CosignVerifier("/k.pub", run=run)
    assert not v.verify(IMAGE, M, P)
    assert v.verify(IMAGE, M, P)
    assert v.verify(IMAGE, M, P)
    assert len(run.argv) == 2


def test_attestation_parser_ignores_garbage():
    assert not attestation_matches("not json\n{}\n", M, P)
