"""Assessments (`contract/dependencies.md` section 5), through the HTTP application."""

from __future__ import annotations

import copy

import pytest

from tests.alias_world import CASES, CLUSTER, AliasWorld, dep_of

SETUP = CASES["setup"]
CANDIDATE = {"engine": "fast", "release": "r2"}
BODY = {"cluster": CLUSTER, "alias": "fast", "candidate": CANDIDATE}


def world() -> AliasWorld:
    w = AliasWorld()
    w.build(SETUP)
    return w


def assess(w: AliasWorld, body=BODY, token="operator"):
    return w.post("/v1/assessments", body, token=token)


def code(answer, status: int) -> str:
    assert answer.status_code == status, answer.text
    return answer.json()["error"]["code"]


def test_an_assessment_lists_the_dependants_of_the_alias_and_is_stored():
    w = world()

    answer = assess(w)

    assert answer.status_code == 200, answer.text
    found = answer.json()
    assert found["from"] == {"engine": "fast", "release": "r1"}
    assert found["candidate"] == CANDIDATE
    assert [d["agent"] for d in found["dependants"]] == [
        "by-hand",
        "fails",
        "no-suite",
        "off",
        "passes",
    ]
    by_agent = {d["agent"]: d for d in found["dependants"]}
    assert {a: d["held"] for a, d in by_agent.items()} == {
        "by-hand": False,
        "fails": True,
        "no-suite": True,
        "off": False,
        "passes": True,
    }
    assert by_agent["passes"]["evaluation"] == "required"
    assert by_agent["no-suite"]["evaluation"] == "none"
    assert by_agent["no-suite"]["needs"] == [{"need": "toolCalling", "met": True}]
    assert by_agent["passes"]["needs"] == []
    assert by_agent["passes"]["release"] == "passes-2026-11-18-aaaaaaaa"
    assert by_agent["passes"]["authority"] == "registry"
    assert found["blocked"] is False
    assert found["digest"].startswith("sha256:")
    assert found["createdBy"] == "al@example.com"
    assert w.store.assessment(found["id"]) is not None
    assert found["stale"] is False


def test_a_candidate_that_does_not_offer_tool_calling_blocks_an_enabled_dependant_that_needs_it():
    w = world()

    r3 = assess(w, BODY | {"candidate": {"engine": "fast", "release": "r3"}}).json()

    assert r3["blocked"] is True
    assert [d["needs"] for d in r3["dependants"] if d["agent"] == "no-suite"] == [
        [{"need": "toolCalling", "met": False}]
    ]


def test_a_manifest_that_does_not_say_leaves_the_need_unknown_and_does_not_block():
    w = AliasWorld()
    setup = copy.deepcopy(SETUP)
    setup["releases"]["r4"] = {"servedName": "fast-four", "offers": {}}
    w.build(setup)

    found = assess(w, BODY | {"candidate": {"engine": "fast", "release": "r4"}}).json()

    assert [d["needs"] for d in found["dependants"] if d["agent"] == "no-suite"] == [
        [{"need": "toolCalling", "met": None}]
    ]
    assert found["blocked"] is False


@pytest.mark.parametrize(
    ("change", "status", "expected"),
    [
        (lambda b: b.update(cluster="elsewhere"), 404, "not-found"),
        (lambda b: b.update(alias="nope"), 404, "not-found"),
        (lambda b: b["candidate"].update(engine="nope"), 404, "not-found"),
        (lambda b: b["candidate"].update(release="nope"), 404, "not-found"),
        (lambda b: b["candidate"].update(release="r1"), 400, "invalid-arguments"),
    ],
    ids=["cluster", "alias", "engine", "release", "the-pair-behind-the-alias-now"],
)
def test_a_candidate_that_is_unknown_or_is_what_the_alias_serves_is_refused(
    change, status, expected
):
    w = world()
    body = copy.deepcopy(BODY)
    change(body)
    before = w.store.generation()

    assert code(assess(w, body), status) == expected

    assert w.store.generation() == before


def test_an_assessment_is_stale_when_computing_it_again_gives_another_digest():
    w = world()
    found = assess(w).json()
    assert w.get(f"/v1/assessments/{found['id']}", token="reader").json()["stale"] is False

    assert (
        w.post(f"/v1/deployments/{dep_of('passes')}/enabled", {"enabled": False}).status_code == 200
    )

    again = w.get(f"/v1/assessments/{found['id']}", token="reader")
    assert again.status_code == 200
    assert again.json()["stale"] is True
    assert again.json()["digest"] == found["digest"]
    assert code(w.get("/v1/assessments/asm-nope", token="reader"), 404) == "not-found"


def test_a_service_token_cannot_create_an_assessment_and_a_reader_can_only_read():
    w = world()

    assert assess(w, token="robot").status_code == 403
    assert assess(w, token="reader").status_code == 403
    assert assess(w, token="nobody").status_code == 403
    assert w.store.assessment("x") is None
    found = assess(w).json()
    assert w.get(f"/v1/assessments/{found['id']}", token="reader").status_code == 200
    assert w.get(f"/v1/assessments/{found['id']}", token="provisioner").status_code == 403
