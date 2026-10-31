from __future__ import annotations

import base64
import json
import subprocess

from agent_platform.signing import (
    PREDICATE_EVAL,
    PREDICATE_RELEASE,
    PREDICATE_SLSA,
    PREDICATE_SPDX,
    PREDICATE_VULN,
    SignerIdentity,
    subject_regexp_for,
)

from registry_controller.signature import (
    CosignVerifier,
    KeylessVerifier,
    attestation_matches,
    signing_identity,
)

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


# -- keyless (private Sigstore) ---------------------------------------------------------

ISSUER = "https://token.actions.githubusercontent.com"
SUBJECT = "https://github.com/X-McKay/zetra-agents/.github/workflows/release.yml@refs/tags/investigator/v1.2.0"
SIGNER = SignerIdentity(
    ISSUER,
    subject_regexp_for("X-McKay/zetra-agents"),
    (PREDICATE_SLSA, PREDICATE_VULN, PREDICATE_EVAL),
)
EVAL_OK = {"suite": "investigator", "commit": "ab" * 20, "passed": True, "total": 3, "failed": 0}
VULN_OK = {
    "scanner": {"result": {"matches": [{"vulnerability": {"id": "CVE-1", "severity": "Low"}}]}}
}


def signed_by(subject: str = SUBJECT, issuer: str = ISSUER) -> str:
    return json.dumps([{"optional": {"Issuer": issuer, "Subject": subject}}])


def statement(predicate_type: str, predicate) -> str:
    body = {"predicateType": predicate_type, "predicate": predicate}
    return json.dumps({"payload": base64.b64encode(json.dumps(body).encode()).decode()}) + "\n"


def keyless(results) -> tuple[KeylessVerifier, Runner]:
    run = Runner(results)
    return KeylessVerifier("/etc/sigstore/trusted_root.json", "https://rekor.example", run=run), run


def all_good():
    return [
        (0, signed_by()),
        (0, statement(PREDICATE_SLSA, {"buildDefinition": {}})),
        (0, statement(PREDICATE_VULN, VULN_OK)),
        (0, statement(PREDICATE_EVAL, EVAL_OK)),
    ]


def test_keyless_verifies_identity_and_every_attestation():
    v, run = keyless(all_good())
    result = v.verify(IMAGE, M, P, SIGNER)
    assert result.ok and result.subject == SUBJECT and result.issuer == ISSUER
    assert result.attestations == SIGNER.required_attestations
    verify = run.argv[0]
    assert verify[:2] == ["cosign", "verify"]
    assert verify[verify.index("--trusted-root") + 1] == "/etc/sigstore/trusted_root.json"
    assert verify[verify.index("--rekor-url") + 1] == "https://rekor.example"
    assert verify[verify.index("--certificate-oidc-issuer") + 1] == ISSUER
    assert verify[verify.index("--certificate-identity-regexp") + 1] == SIGNER.subject_regexp
    assert "--key" not in verify
    types = [a[a.index("--type") + 1] for a in run.argv[1:]]
    assert types == list(SIGNER.required_attestations)


def test_keyless_without_signer_fails_closed():
    v, run = keyless([])
    assert v.verify(IMAGE, M, P, None).reason == "SignerIdentityMissing"
    assert run.argv == []


def test_keyless_rechecks_identity_cosign_reports():
    # Even if cosign accepted the flags, a subject outside the declaration is refused.
    other = SUBJECT.replace("zetra-agents", "evil-agents")
    v, _ = keyless([(0, signed_by(other))])
    assert v.verify(IMAGE, M, P, SIGNER).reason == "SignerMismatch"
    branch = SUBJECT.replace("refs/tags/investigator/v1.2.0", "refs/heads/main")
    v, _ = keyless([(0, signed_by(branch))])
    assert v.verify(IMAGE, M, P, SIGNER).reason == "SignerMismatch"


def test_keyless_missing_or_failing_attestation():
    results = all_good()
    results[2] = (1, "")
    v, _ = keyless(results)
    assert v.verify(IMAGE, M, P, SIGNER).reason == "AttestationMissing"

    results = all_good()
    results[3] = (0, statement(PREDICATE_EVAL, dict(EVAL_OK, failed=1, passed=False)))
    v, _ = keyless(results)
    assert v.verify(IMAGE, M, P, SIGNER).reason == "AttestationRejected"

    critical = {
        "scanner": {
            "result": {"matches": [{"vulnerability": {"id": "CVE-9", "severity": "Critical"}}]}
        }
    }
    results = all_good()
    results[2] = (0, statement(PREDICATE_VULN, critical))
    v, _ = keyless(results)
    assert v.verify(IMAGE, M, P, SIGNER).reason == "AttestationRejected"


def test_keyless_predicate_type_must_match():
    results = all_good()
    results[3] = (0, statement(PREDICATE_SLSA, EVAL_OK))  # right body, wrong type
    v, _ = keyless(results)
    assert not v.verify(IMAGE, M, P, SIGNER)


def test_keyless_cache_is_keyed_on_signer():
    v, run = keyless(all_good() + all_good())
    assert v.verify(IMAGE, M, P, SIGNER)
    assert v.verify(IMAGE, M, P, SIGNER)
    assert len(run.argv) == 4
    narrower = SignerIdentity(ISSUER, SIGNER.subject_regexp, (PREDICATE_SLSA,))
    assert v.verify(IMAGE, M, P, narrower)
    assert len(run.argv) == 6


def test_signing_identity_needs_one_consistent_identity():
    assert signing_identity(signed_by()) == (ISSUER, SUBJECT)
    two = json.dumps(
        [
            {"optional": {"Issuer": ISSUER, "Subject": SUBJECT}},
            {"optional": {"Issuer": ISSUER, "Subject": SUBJECT + "x"}},
        ]
    )
    assert signing_identity(two) is None
    assert signing_identity("[{}]") is None
    assert signing_identity("nope") is None


def test_keyless_every_predicate_of_a_type_must_pass():
    # A later rescan that found a critical CVE is not masked by the earlier clean scan.
    critical = {
        "scanner": {
            "result": {"matches": [{"vulnerability": {"id": "CVE-9", "severity": "Critical"}}]}
        }
    }
    results = all_good()
    results[2] = (0, statement(PREDICATE_VULN, VULN_OK) + statement(PREDICATE_VULN, critical))
    v, _ = keyless(results)
    assert v.verify(IMAGE, M, P, SIGNER).reason == "AttestationRejected"
    results = all_good()
    results[3] = (
        0,
        statement(PREDICATE_EVAL, EVAL_OK) + statement(PREDICATE_EVAL, dict(EVAL_OK, failed=1)),
    )
    v, _ = keyless(results)
    assert v.verify(IMAGE, M, P, SIGNER).reason == "AttestationRejected"


def test_keyless_default_declaration_binds_manifest_digest():
    default = SignerIdentity(ISSUER, subject_regexp_for("X-McKay/zetra-agents"))
    assert PREDICATE_RELEASE in default.required_attestations

    def run_with(manifest):
        results = [(0, signed_by())]
        for t in default.required_attestations:
            body = {
                PREDICATE_SLSA: {"buildDefinition": {}},
                PREDICATE_SPDX: {"spdxVersion": "SPDX-2.3"},
                PREDICATE_VULN: VULN_OK,
                PREDICATE_EVAL: EVAL_OK,
                PREDICATE_RELEASE: {"manifestSha256": manifest, "policySha256": P},
            }[t]
            results.append((0, statement(t, body)))
        v, _ = keyless(results)
        return v.verify(IMAGE, M, P, default)

    assert run_with(M).ok
    assert run_with("00" * 32).reason == "AttestationRejected"
