import datetime as dt
import json
from pathlib import Path

import pytest
from agent_platform import signing

from zetra_agents import attestations, cli

SHA = "ab" * 20
JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4" failures="{failures}" errors="0" skipped="1">
  <testcase classname="tests.test_agent" name="test_tools"/>
  <testcase classname="tests.test_agent" name="test_manifest"/>
  <testcase classname="tests.test_agent" name="test_slow"><skipped message="slow"/></testcase>
  <testcase classname="tests.test_agent" name="test_budget">{failure}</testcase>
</testsuite></testsuites>
"""
GRYPE = {
    "matches": [
        {"vulnerability": {"id": "CVE-2026-0001", "severity": "High"}},
        {"vulnerability": {"id": "CVE-2026-0002", "severity": "Low"}},
    ],
    "descriptor": {
        "name": "grype",
        "version": "0.120.1",
        "db": {"status": {"schemaVersion": "v6.1.2", "from": "https://grype.anchore.io/x"}},
    },
}
GITHUB_ENV = {
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "X-McKay/zetra-agents",
    "GITHUB_REF": "refs/tags/investigator/v0.1.1",
    "GITHUB_WORKFLOW_REF": "X-McKay/zetra-agents/.github/workflows/release.yml"
    "@refs/tags/investigator/v0.1.1",
    "GITHUB_RUN_ID": "42",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_EVENT_NAME": "push",
    "RUNNER_ENVIRONMENT": "self-hosted",
}


def _junit(tmp_path: Path, failing: bool) -> Path:
    path = tmp_path / "junit.xml"
    failure = '<failure message="boom"/>' if failing else ""
    path.write_text(JUNIT.format(failures=int(failing), failure=failure))
    return path


def test_eval_predicate_from_a_passing_suite_satisfies_the_contract(tmp_path):
    p = attestations.eval_predicate(_junit(tmp_path, False), suite="agents/x", commit=SHA)
    assert (p["total"], p["failed"], p["skipped"], p["passed"]) == (3, 0, 1, True)
    assert signing.eval_problems(p, commit=SHA) == []


def test_eval_predicate_records_failures_and_the_contract_rejects_it(tmp_path):
    p = attestations.eval_predicate(_junit(tmp_path, True), suite="agents/x", commit=SHA)
    assert (p["failed"], p["passed"]) == (1, False)
    assert signing.eval_problems(p)


def test_an_empty_suite_does_not_pass(tmp_path):
    path = tmp_path / "junit.xml"
    path.write_text('<testsuites><testsuite name="pytest" tests="0"/></testsuites>')
    p = attestations.eval_predicate(path, suite="agents/x", commit=SHA)
    assert p["passed"] is False and signing.eval_problems(p)


def test_vuln_predicate_wraps_grype_and_the_contract_reads_its_findings():
    t = dt.datetime(2026, 11, 1, 12, tzinfo=dt.UTC)
    p = attestations.vuln_predicate(GRYPE, started=t, finished=t)
    assert p["scanner"]["version"] == "0.120.1"
    assert p["scanner"]["db"]["version"] == "v6.1.2"
    assert p["metadata"]["scanStartedOn"] == "2026-11-01T12:00:00Z"
    assert signing.vuln_findings(p) == [("CVE-2026-0001", "high"), ("CVE-2026-0002", "low")]
    assert signing.vuln_problems(p) == ["high vulnerability CVE-2026-0001 without an exception"]
    assert signing.vuln_problems(p, ("CVE-2026-0001",)) == []


def test_vuln_predicate_refuses_a_non_grype_report():
    t = dt.datetime.now(dt.UTC)
    with pytest.raises(ValueError):
        attestations.vuln_predicate({"Results": []}, started=t, finished=t)


def test_provenance_names_the_workflow_identity_fulcio_certifies():
    p = attestations.slsa_provenance(GITHUB_ENV, revision=SHA)
    build = p["buildDefinition"]
    assert build["externalParameters"]["workflow"] == {
        "ref": "refs/tags/investigator/v0.1.1",
        "repository": "https://github.com/X-McKay/zetra-agents",
        "path": ".github/workflows/release.yml",
    }
    assert build["resolvedDependencies"][0]["digest"] == {"gitCommit": SHA}
    builder = p["runDetails"]["builder"]["id"]
    # The same string as the Fulcio certificate SAN, so the tenant pin matches it.
    pin = signing.subject_regexp_for("X-McKay/zetra-agents", agents=("investigator",))
    assert signing.SignerIdentity(signing.GITHUB_ACTIONS_ISSUER, pin).matches(
        signing.GITHUB_ACTIONS_ISSUER, builder
    )
    assert p["runDetails"]["metadata"]["invocationId"].endswith("/runs/42/attempts/2")


def test_provenance_outside_actions_is_refused():
    with pytest.raises(ValueError, match="GITHUB_REPOSITORY"):
        attestations.slsa_provenance({}, revision=SHA)


def test_cli_writes_each_predicate(tmp_path, monkeypatch, capsys):
    for key, value in GITHUB_ENV.items():
        monkeypatch.setenv(key, value)
    grype = tmp_path / "grype.json"
    grype.write_text(json.dumps(GRYPE))
    junit = _junit(tmp_path, False)
    runs = {
        "eval": ["--junit", str(junit), "--suite", "agents/x", "--commit", SHA],
        "vuln": [
            "--grype",
            str(grype),
            "--started",
            "2026-11-01T12:00:00+00:00",
            "--finished",
            "2026-11-01T12:01:00+00:00",
        ],
        "provenance": ["--revision", SHA],
    }
    for kind, extra in runs.items():
        out = tmp_path / f"{kind}.json"
        assert cli.main(["predicate", kind, *extra, "--out", str(out)]) == 0
        assert json.loads(out.read_text())
    vuln = json.loads((tmp_path / "vuln.json").read_text())
    assert vuln["invocation"]["uri"] == "https://github.com/X-McKay/zetra-agents/actions/runs/42"
