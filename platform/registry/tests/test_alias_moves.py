"""The alias move routes (`contract/dependencies.md` section 6.3) and what only they decide:
who may call them, the evaluation window, and what a move says it waits for. The scenarios of
`alias-move-cases.json` are in `test_alias_move_cases.py`."""

from __future__ import annotations

import pytest

from agent_registry.config import Settings
from tests.alias_world import CASES, CLUSTER, AliasWorld, dep_of

CANDIDATE = {"engine": "fast", "release": "r2"}
BODY = {"cluster": CLUSTER, "alias": "fast", "candidate": CANDIDATE}


def world(**settings) -> AliasWorld:
    w = AliasWorld()
    if settings:
        w.settings = Settings(**settings)
        w.app.state.settings = w.settings
    w.build(CASES["setup"])
    return w


def assessed(w: AliasWorld) -> str:
    return w.post("/v1/assessments", BODY).json()["id"]


def started(w: AliasWorld, **body):
    return w.post("/v1/alias-moves", {"assessment": assessed(w)} | body)


def code(answer, status: int) -> str:
    assert answer.status_code == status, answer.text
    return answer.json()["error"]["code"]


def test_a_move_starts_with_a_hold_per_held_dependant_and_says_what_it_waits_for():
    w = world()

    answer = started(w)

    assert answer.status_code == 200, answer.text
    move = answer.json()
    assert move["state"] == "holding"
    assert move["sessions"] == "finish"
    assert (
        move["evaluationSeconds"] == 1800
    )  # the default of REGISTRY_ALIAS_MOVE_EVALUATION_SECONDS
    assert move["candidate"] == CANDIDATE
    assert {h["agent"]: h["state"] for h in move["holds"]} == {
        "fails": "holding",
        "no-suite": "holding",
        "passes": "holding",
    }
    assert move["next"] == "waiting for 3 agent(s) to stop: fails, no-suite, passes"
    assert w.get(f"/v1/alias-moves/{move['id']}", token="reader").json() == move
    listed = w.get("/v1/alias-moves", token="reader", cluster=CLUSTER, alias="fast", open="true")
    assert [m["id"] for m in listed.json()["moves"]] == [move["id"]]
    assert w.get("/v1/alias-moves", token="reader", open="false").json() == {"moves": []}


def test_the_evaluation_window_comes_from_the_request_or_the_setting_within_60_to_86400():
    w = world(alias_move_evaluation_seconds=900)

    assert started(w).json()["evaluationSeconds"] == 900
    assert code(started(w, evaluationSeconds=59), 400) == "invalid-arguments"
    assert code(started(w, evaluationSeconds=86401), 400) == "invalid-arguments"
    other = world()
    assert started(other, evaluationSeconds=60).json()["evaluationSeconds"] == 60
    assert started(world(), evaluationSeconds=86400).json()["evaluationSeconds"] == 86400


def test_a_move_with_no_agent_to_hold_is_held_at_once():
    w = AliasWorld()
    w.build({**CASES["setup"], "agents": [CASES["setup"]["agents"][3]]})  # by-hand only

    move = started(w).json()

    assert (move["state"], move["holds"]) == ("held", [])
    assert move["next"] == "merge the change that moves the alias"


def test_a_service_token_cannot_start_evaluate_or_cancel_a_move_or_assess():
    w = world()
    asm = assessed(w)
    move = w.post("/v1/alias-moves", {"assessment": asm}).json()["id"]

    for token in ("robot", "reader", "provisioner", "evaluator"):
        assert w.post("/v1/assessments", BODY, token=token).status_code == 403
        assert w.post("/v1/alias-moves", {"assessment": asm}, token=token).status_code == 403
        assert w.post(f"/v1/alias-moves/{move}/evaluate", token=token).status_code == 403
        assert w.post(f"/v1/alias-moves/{move}/cancel", {}, token=token).status_code == 403
    assert w.store.alias_move(move).state == "holding"
    assert w.get(f"/v1/alias-moves/{move}", token="reader").status_code == 200


def test_a_move_names_an_assessment_that_exists_and_a_move_that_exists():
    w = world()

    assert code(w.post("/v1/alias-moves", {"assessment": "asm-nope"}), 404) == "not-found"
    assert code(w.get("/v1/alias-moves/mv-nope", token="reader"), 404) == "not-found"
    assert code(w.post("/v1/alias-moves/mv-nope/cancel", {}), 404) == "not-found"
    assert code(w.post("/v1/alias-moves/mv-nope/evaluate"), 404) == "not-found"


def test_a_move_that_is_not_moving_cannot_be_evaluated_and_one_that_is_cancelled_says_so():
    w = world()
    move = started(w).json()["id"]

    assert code(w.post(f"/v1/alias-moves/{move}/evaluate"), 409) == "refused"
    cancelled = w.post(f"/v1/alias-moves/{move}/cancel", {"reason": "not today"})
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert cancelled.json()["next"] == "cancelled: not today"
    assert code(w.post(f"/v1/alias-moves/{move}/cancel", {}), 409) == "refused"
    assert {h["state"] for h in cancelled.json()["holds"]} == {"released"}


def test_a_held_agent_has_its_generation_raised_so_that_its_agent_is_written_again():
    w = world()
    before = {a: w.store.deployment(dep_of(a)).generation for a in ("passes", "by-hand", "off")}

    started(w)

    after = {a: w.store.deployment(dep_of(a)).generation for a in before}
    assert after["passes"] > before["passes"]
    assert after["by-hand"] == before["by-hand"] and after["off"] == before["off"]


@pytest.mark.parametrize("token", ["reader", "operator"])
def test_the_check_is_for_a_reader_and_writes_nothing(token):
    w = world()
    before = w.store.generation()

    answer = w.post("/v1/checks/alias", BODY, token=token)

    assert answer.status_code == 200, answer.text
    assert answer.json()["allowed"] is False
    assert w.store.generation() == before
    assert w.post("/v1/checks/alias", BODY, token="provisioner").status_code == 403
    assert (
        code(w.post("/v1/checks/alias", BODY | {"alias": "nope"}, token=token), 404) == "not-found"
    )
