"""The release report (`contract/delivery.md` section 3), through the HTTP application, and
the suite and declaration rules of sections 1.3 and 2."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agent_registry import modes
from agent_registry.api.github import GithubClaims, StaticGithubVerifier
from agent_registry.config import Settings
from agent_registry.rules import Refusal, report
from tests.world import AGENT, DEP, REPOSITORY, World, published, registry_owned

REPORTS = "/v1/reports/releases"
WORKFLOW = f"{REPOSITORY}/.github/workflows/release.yml@refs/tags/db-admin/v0.2.10"
CASES = json.loads((Path(__file__).parents[1] / "contract" / "suite-cases.json").read_text())


def claims(**changes) -> GithubClaims:
    base = {
        "repository": REPOSITORY,
        "ref": f"refs/tags/{AGENT}/v0.2.10",
        "ref_type": "tag",
        "sha": "c" * 40,
        "job_workflow_ref": WORKFLOW,
    }
    return GithubClaims(**{**base, **changes})


def reporting(**tokens: GithubClaims) -> World:
    """db-admin on the registry path, and a GitHub verifier that knows these tokens."""
    world = registry_owned()
    world.settings = Settings(report_audience="agent-registry-reports")
    world.app.state.settings = world.settings
    world.app.state.github_verifier = StaticGithubVerifier({"good": claims(), **tokens})
    return world


def body(letter: str = "a", **extra) -> dict:
    sent = published(letter)
    sent.pop("lineage")
    return {**sent, "lineage": {"checksums": {"agent.yaml": "sha256:" + "1" * 64}}, **extra}


def send(world: World, document: dict, token: str = "good"):
    return world.post(REPORTS, document, token=token)


def test_a_report_from_an_unknown_repository_is_refused():
    world = reporting(stranger=claims(repository="someone/else"))

    answer = send(world, body(), "stranger")

    assert answer.status_code == 403
    assert answer.json()["error"]["code"] == "refused"


def test_the_same_report_twice_makes_one_release_and_one_revision():
    world = reporting()
    before = len(world.store.revisions(DEP))

    first, second = send(world, body()), send(world, body())

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["created"] is True and second.json()["created"] is False
    assert first.json()["revision"] == second.json()["revision"] == before + 1
    assert first.json()["deployment"] == DEP
    assert len(world.store.revisions(DEP)) == before + 1


def test_a_report_cannot_set_an_approval():
    world = reporting()
    document = body()
    document["agentRelease"]["spec"]["approval"] = {"approved": True, "approvedBy": "al"}

    refused = send(world, document)
    accepted = send(world, body("b"))

    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid-arguments"
    newest = world.store.revision(DEP, accepted.json()["revision"])
    assert newest.approval_id is None
    assert len(world.store.revisions(DEP)) == accepted.json()["revision"]


@pytest.mark.parametrize(
    ("token", "status"),
    [
        (claims(ref="refs/heads/main", ref_type="branch"), 403),
        # The tag names an agent the documents do not: refused as the table says.
        (claims(ref="refs/tags/investigator/v0.2.10"), 400),
        (
            claims(
                job_workflow_ref=f"{REPOSITORY}/.github/workflows/other.yml@refs/tags/db-admin/v1"
            ),
            403,
        ),
    ],
    ids=["branch", "another-agent", "another-workflow"],
)
def test_a_token_for_a_branch_another_agent_or_another_workflow_is_refused(token, status):
    world = reporting(bad=token)

    answer = send(world, body(), "bad")

    assert answer.status_code == status
    assert len(world.store.revisions(DEP)) == 2


def test_an_agent_the_repository_is_not_trusted_for_is_refused():
    world = reporting(
        bad=claims(
            ref="refs/tags/unlisted/v1.0.0",
            job_workflow_ref=f"{REPOSITORY}/.github/workflows/release.yml@refs/tags/unlisted/v1.0.0",
        )
    )

    answer = send(world, body(), "bad")

    assert answer.status_code == 403


def test_the_lineage_is_the_tokens_whatever_the_body_says():
    world = reporting()
    document = body()
    document["lineage"].update(
        repository="evil/repo", tag="db-admin/v9.9.9", commit="f" * 40, workflow="evil"
    )

    answer = send(world, document)

    release = world.store.release(world.store.revision(DEP, answer.json()["revision"]).release_id)
    assert answer.status_code == 200, answer.text
    assert release.lineage == {
        "repository": REPOSITORY,
        "tag": "db-admin/v0.2.10",
        "commit": "c" * 40,
        "workflow": WORKFLOW,
        "checksums": {"agent.yaml": "sha256:" + "1" * 64},
    }
    (event,) = [e for e in world.store.events(deployment_id=DEP) if e.type == "release-recorded"]
    assert event.actor == f"github:{REPOSITORY}@db-admin/v0.2.10"


def test_the_deployment_must_be_on_the_registry_path_known_and_not_in_recovery():
    world = reporting()
    on_pull_request = World()
    on_pull_request.seed()
    on_pull_request.settings = world.settings
    on_pull_request.app.state.settings = world.settings
    on_pull_request.app.state.github_verifier = world.app.state.github_verifier
    unknown = reporting(
        other=claims(
            ref="refs/tags/conformance/v1.0.0",
            job_workflow_ref=f"{REPOSITORY}/.github/workflows/release.yml@refs/tags/conformance/v1.0.0",
        )
    )
    modes.enter_recovery(world.store, "test", actor="test")

    wrong = send(on_pull_request, body())
    missing_document = body()
    missing_document["agent"]["metadata"]["name"] = "conformance"
    missing_document["agentRelease"]["spec"]["agent"] = "conformance"
    missing = send(unknown, missing_document, "other")
    recovering = send(world, body())

    assert (wrong.status_code, wrong.json()["error"]["code"]) == (409, "wrong-path")
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "not-found")
    assert (recovering.status_code, recovering.json()["error"]["code"]) == (409, "in-recovery")


def test_the_documents_must_name_the_agent_of_the_tag_in_the_trusted_namespace():
    world = reporting()
    wrong_name, wrong_namespace = body(), body("b")
    wrong_name["agentRelease"]["spec"]["agent"] = "investigator"
    wrong_namespace["agent"]["metadata"]["namespace"] = "elsewhere"

    names, namespaces = send(world, wrong_name), send(world, wrong_namespace)

    assert (names.status_code, names.json()["error"]["code"]) == (400, "invalid-arguments")
    assert (namespaces.status_code, namespaces.json()["error"]["code"]) == (
        400,
        "invalid-arguments",
    )


@pytest.mark.parametrize("case", CASES["valid"], ids=lambda c: c["name"])
def test_every_valid_suite_is_accepted(case):
    report.check_suite(case["suite"])
    world = reporting()

    answer = send(world, body(evaluation={"suite": case["suite"]}))

    assert answer.status_code == 200, answer.text
    release = world.store.release(world.store.revision(DEP, answer.json()["revision"]).release_id)
    assert release.lineage["evaluation"] == {"suite": case["suite"]}


@pytest.mark.parametrize("case", CASES["invalid"], ids=lambda c: c["name"])
def test_every_invalid_suite_is_refused_naming_the_key(case):
    with pytest.raises(Refusal) as refused:
        report.check_suite(case["suite"])
    world = reporting()

    answer = send(world, body(evaluation={"suite": case["suite"]}))

    assert refused.value.code == "invalid-arguments"
    assert case["mentions"] in refused.value.message
    assert answer.status_code == 400
    assert case["mentions"] in answer.json()["error"]["message"]
    assert len(world.store.revisions(DEP)) == 2


def test_a_carried_declaration_must_be_the_agents_spec_without_enabled():
    world = reporting()
    equal, other, absent = body("a"), body("b"), body("c")
    spec = copy.deepcopy(equal["agent"]["spec"])
    equal["agentRelease"]["spec"]["declaration"] = {k: v for k, v in spec.items() if k != "enabled"}
    other["agentRelease"]["spec"]["declaration"] = {**spec, "description": "something else"}

    accepted, refused, without = send(world, equal), send(world, other), send(world, absent)

    assert accepted.status_code == 200, accepted.text
    assert (refused.status_code, refused.json()["error"]["code"]) == (400, "invalid-arguments")
    assert "declaration" in refused.json()["error"]["message"]
    assert without.status_code == 200


def test_with_no_report_audience_the_route_is_off():
    world = reporting()
    world.settings = Settings()
    world.app.state.settings = world.settings

    answer = send(world, body())

    assert (answer.status_code, answer.json()["error"]["code"]) == (404, "not-found")
