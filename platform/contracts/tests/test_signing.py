from __future__ import annotations

import json
import re

import pytest
import yaml

from agent_platform.bundle import compile_bundle, digest_hex
from agent_platform.cli import main as agentctl
from agent_platform.signing import (
    ANNOTATION_REQUIRED_ATTESTATIONS,
    ANNOTATION_SIGNER_ISSUER,
    ANNOTATION_SIGNER_SUBJECT_REGEXP,
    DEFAULT_REQUIRED_ATTESTATIONS,
    GITHUB_ACTIONS_ISSUER,
    PREDICATE_SLSA,
    SignerIdentity,
    eval_problems,
    subject_regexp_for,
    vuln_problems,
)
from tests.conftest import FIXTURES

REPO = "X-McKay/zetra-agents"
TAG_SUBJECT = (
    f"https://github.com/{REPO}/.github/workflows/release.yml@refs/tags/investigator/v1.2.0"
)
EVAL = {"suite": "investigator", "commit": "ab" * 20, "passed": True, "total": 4, "failed": 0}


def grype(*findings):
    matches = [{"vulnerability": {"id": i, "severity": s}} for i, s in findings]
    return {"scanner": {"uri": "grype", "result": {"matches": matches}}}


def test_subject_regexp_accepts_only_release_tags_of_that_repo():
    pattern = subject_regexp_for(REPO)
    assert re.fullmatch(pattern, TAG_SUBJECT)
    for other in (
        TAG_SUBJECT.replace("refs/tags/investigator/v1.2.0", "refs/heads/main"),
        TAG_SUBJECT.replace("zetra-agents", "zetra-agents-fork"),
        TAG_SUBJECT.replace("release.yml", "other.yml"),
        TAG_SUBJECT.replace("github.com", "github.com.evil"),
        TAG_SUBJECT + "\nx",
    ):
        assert not re.fullmatch(pattern, other), other
    with pytest.raises(ValueError):
        subject_regexp_for("not a repo")


def test_identity_from_annotations_round_trips_and_defaults():
    identity = SignerIdentity(GITHUB_ACTIONS_ISSUER, subject_regexp_for(REPO))
    parsed = SignerIdentity.from_annotations(identity.annotations())
    assert parsed == identity
    assert parsed.required_attestations == DEFAULT_REQUIRED_ATTESTATIONS
    assert parsed.matches(GITHUB_ACTIONS_ISSUER, TAG_SUBJECT)
    assert not parsed.matches("https://accounts.google.com", TAG_SUBJECT)
    narrow = SignerIdentity.from_annotations(
        {
            ANNOTATION_SIGNER_ISSUER: GITHUB_ACTIONS_ISSUER,
            ANNOTATION_SIGNER_SUBJECT_REGEXP: subject_regexp_for(REPO),
            ANNOTATION_REQUIRED_ATTESTATIONS: f" {PREDICATE_SLSA} ,",
        }
    )
    assert narrow.required_attestations == (PREDICATE_SLSA,)


@pytest.mark.parametrize(
    "annotations",
    [
        {},
        {ANNOTATION_SIGNER_ISSUER: GITHUB_ACTIONS_ISSUER},
        {ANNOTATION_SIGNER_ISSUER: "http://x", ANNOTATION_SIGNER_SUBJECT_REGEXP: "^a$"},
        {ANNOTATION_SIGNER_ISSUER: GITHUB_ACTIONS_ISSUER, ANNOTATION_SIGNER_SUBJECT_REGEXP: "a"},
        {ANNOTATION_SIGNER_ISSUER: GITHUB_ACTIONS_ISSUER, ANNOTATION_SIGNER_SUBJECT_REGEXP: "^(a$"},
        {
            ANNOTATION_SIGNER_ISSUER: GITHUB_ACTIONS_ISSUER,
            ANNOTATION_SIGNER_SUBJECT_REGEXP: "^a$",
            ANNOTATION_REQUIRED_ATTESTATIONS: "https://example.com/made-up",
        },
    ],
)
def test_bad_declarations_are_refused(annotations):
    with pytest.raises(ValueError):
        SignerIdentity.from_annotations(annotations)
    with pytest.raises(ValueError):
        SignerIdentity.from_namespace(
            {"metadata": {"annotations": annotations}} if annotations else None
        )


def test_eval_conditions():
    assert eval_problems(EVAL) == []
    assert eval_problems(EVAL, commit="ab" * 20) == []
    assert eval_problems(EVAL, commit="cd" * 20)
    assert eval_problems(dict(EVAL, failed=1))
    assert eval_problems(dict(EVAL, passed=False))
    assert eval_problems(dict(EVAL, total=0))
    assert eval_problems(dict(EVAL, failed=False))  # bool is not an int here
    assert eval_problems(dict(EVAL, commit="main"))
    assert eval_problems(dict(EVAL, results=[{"name": "t1", "passed": False}]))
    assert eval_problems("passed")


def test_vuln_conditions():
    assert vuln_problems(grype(("CVE-1", "Low"), ("CVE-2", "Medium"))) == []
    assert vuln_problems(grype(("CVE-3", "Critical")))
    assert vuln_problems(grype(("CVE-4", "High")))
    assert vuln_problems(grype(("CVE-4", "High")), exceptions=("CVE-4",)) == []
    assert vuln_problems(grype(("CVE-5", "Critical")), exceptions=("CVE-5",))  # never waivable
    trivy = {
        "scanner": {
            "result": {
                "Results": [
                    {"Vulnerabilities": [{"VulnerabilityID": "CVE-6", "Severity": "CRITICAL"}]}
                ]
            }
        }
    }
    assert vuln_problems(trivy)
    assert vuln_problems({"scanner": {"result": {}}})
    assert vuln_problems({})


# -- agentctl validate / signer-annotations --------------------------------------------


@pytest.fixture
def release_files(tmp_path, cfg):
    agent = yaml.safe_load((FIXTURES / "agent.yaml").read_text())
    release = yaml.safe_load((FIXTURES / "release.yaml").read_text())
    release["spec"]["policySha256"] = digest_hex(
        compile_bundle(
            agent, release, mcp_resource_base=cfg.mcp_resource_base, trust_domain=cfg.trust_domain
        )
    )

    def write(rel=release, ag=agent):
        (tmp_path / "agent.yaml").write_text(yaml.safe_dump(ag))
        (tmp_path / "release.yaml").write_text(yaml.safe_dump(rel))
        return [
            "validate",
            "--agent",
            str(tmp_path / "agent.yaml"),
            "--release",
            str(tmp_path / "release.yaml"),
            "--mcp-resource-base",
            cfg.mcp_resource_base,
        ]

    return write, agent, release, tmp_path


def test_validate_passes_and_renders(release_files, capsys):
    write, _, _, tmp = release_files
    assert agentctl([*write(), "--out", str(tmp / "out")]) == 0
    assert capsys.readouterr().out.strip() == "OK"
    pod = yaml.safe_load((tmp / "out" / "pod.yaml").read_text())
    assert pod["spec"]["runtimeClassName"] == "kata-qemu-runtime-rs"
    assert len(list(yaml.safe_load_all((tmp / "out" / "netpol.yaml").read_text()))) == 4


def test_validate_reports_each_problem(release_files, capsys):
    write, agent, release, _ = release_files
    bad = json.loads(json.dumps(release))
    bad["spec"]["image"] = "registry.example.com/agents/investigator:latest"
    bad["spec"]["policySha256"] = "0" * 64
    assert agentctl(write(bad)) == 1
    err = capsys.readouterr().err
    assert "pinned" in err and "policySha256 should be" in err


def test_validate_checks_local_predicates(release_files, tmp_path, capsys):
    write, *_ = release_files
    (tmp_path / "eval.json").write_text(json.dumps(dict(EVAL, failed=2, passed=False)))
    (tmp_path / "vuln.json").write_text(json.dumps(grype(("CVE-7", "High"))))
    args = [*write(), "--eval", str(tmp_path / "eval.json"), "--vuln", str(tmp_path / "vuln.json")]
    assert agentctl(args) == 1
    err = capsys.readouterr().err
    assert "eval:" in err and "CVE-7" in err
    assert (
        agentctl([*args[:-4], "--vuln", str(tmp_path / "vuln.json"), "--vuln-exception", "CVE-7"])
        == 0
    )


def test_signer_annotations(capsys):
    assert agentctl(["signer-annotations", "--repo", REPO]) == 0
    doc = yaml.safe_load(capsys.readouterr().out)["annotations"]
    identity = SignerIdentity.from_annotations(doc)
    assert identity.matches(GITHUB_ACTIONS_ISSUER, TAG_SUBJECT)
