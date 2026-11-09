"""Predicates for the attestations Kubani's Kyverno Rule 1 requires (besides release/v1).

`release.sh` attests each of these keylessly next to the release predicate:

| predicate type                                   | built from                       |
| ------------------------------------------------ | -------------------------------- |
| https://slsa.dev/provenance/v1                   | the GitHub Actions run           |
| https://spdx.dev/Document                        | syft's SPDX JSON (as is)         |
| https://cosign.sigstore.dev/attestation/vuln/v1  | grype's JSON report              |
| https://agentplatform.io/eval/v1                 | the agent's pytest JUnit report  |

The eval and vuln shapes are the ones `agent_platform.signing` checks and Kyverno's
conditions read (`scanner.result.matches[].vulnerability.severity`, `passed`/`failed`/
`total`).
"""

from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path
from typing import Any

GITHUB_BUILD_TYPE = "https://actions.github.io/buildtypes/workflow/v1"


def eval_predicate(junit_xml: Path, *, suite: str, commit: str) -> dict[str, Any]:
    """eval/v1 from a pytest `--junitxml` report. Skipped cases are counted, not judged."""
    root = ET.parse(junit_xml).getroot()
    cases = root.iter("testcase")
    results, skipped = [], 0
    for case in cases:
        if case.find("skipped") is not None:
            skipped += 1
            continue
        name = "::".join(p for p in (case.get("classname"), case.get("name")) if p)
        failed = case.find("failure") is not None or case.find("error") is not None
        results.append({"name": name, "passed": not failed})
    failed = sum(1 for r in results if not r["passed"])
    return {
        "suite": suite,
        "commit": commit,
        "passed": failed == 0 and len(results) > 0,
        "total": len(results),
        "failed": failed,
        "skipped": skipped,
        "results": results,
    }


def vuln_predicate(
    grype_report: Mapping[str, Any],
    *,
    started: dt.datetime,
    finished: dt.datetime,
    invocation_uri: str = "",
    builder_id: str = "",
) -> dict[str, Any]:
    """cosign vuln/v1 (CosignVulnPredicate) wrapping a grype JSON report as scanner.result."""
    if "matches" not in grype_report:
        raise ValueError("not a grype JSON report: no 'matches'")
    descriptor = grype_report.get("descriptor") or {}
    version = str(descriptor.get("version", ""))
    db = descriptor.get("db") or {}
    status = db.get("status") or db  # grype >= 0.88 nests the DB fields under "status"
    return {
        "invocation": {
            "parameters": None,
            "uri": invocation_uri,
            "event_id": "",
            "builder.id": builder_id,
        },
        "scanner": {
            "uri": f"pkg:github/anchore/grype@v{version.lstrip('v')}" if version else "",
            "version": version,
            "db": {
                "uri": str(status.get("from", "")),
                "version": str(status.get("schemaVersion", status.get("built", ""))),
            },
            "result": dict(grype_report),
        },
        "metadata": {"scanStartedOn": _rfc3339(started), "scanFinishedOn": _rfc3339(finished)},
    }


def slsa_provenance(env: Mapping[str, str], *, revision: str) -> dict[str, Any]:
    """SLSA provenance v1 for a GitHub Actions run, in the shape GitHub's own builder uses."""
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repo = _need(env, "GITHUB_REPOSITORY")
    ref = _need(env, "GITHUB_REF")
    workflow_ref = _need(env, "GITHUB_WORKFLOW_REF")  # owner/repo/.github/workflows/x.yml@ref
    path = workflow_ref.split("@", 1)[0].removeprefix(repo + "/")
    run = f"{server}/{repo}/actions/runs/{_need(env, 'GITHUB_RUN_ID')}"
    return {
        "buildDefinition": {
            "buildType": GITHUB_BUILD_TYPE,
            "externalParameters": {
                "workflow": {"ref": ref, "repository": f"{server}/{repo}", "path": path}
            },
            "internalParameters": {
                "github": {
                    "event_name": env.get("GITHUB_EVENT_NAME", ""),
                    "repository_id": env.get("GITHUB_REPOSITORY_ID", ""),
                    "repository_owner_id": env.get("GITHUB_REPOSITORY_OWNER_ID", ""),
                    "runner_environment": env.get("RUNNER_ENVIRONMENT", ""),
                }
            },
            "resolvedDependencies": [
                {"uri": f"git+{server}/{repo}@{ref}", "digest": {"gitCommit": revision}}
            ],
        },
        "runDetails": {
            "builder": {"id": f"{server}/{workflow_ref}"},
            "metadata": {"invocationId": f"{run}/attempts/{env.get('GITHUB_RUN_ATTEMPT', '1')}"},
        },
    }


def _need(env: Mapping[str, str], key: str) -> str:
    value = env.get(key)
    if not value:
        raise ValueError(f"{key} is not set; provenance is only built inside GitHub Actions")
    return value


def _rfc3339(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
