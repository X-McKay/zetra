"""What alias moves add to milestone 2's routes, and what they must leave alone: the `held`
annotation, `holds` in the desired document and in the observation report, the wait before D6
and D8, status, and enabling. The scenarios themselves are in `test_alias_move_cases.py`."""

from __future__ import annotations

from agent_registry.config import ANNOTATION_PREFIX
from tests.alias_world import CASES, CLUSTER, AliasWorld, dep_of
from tests.test_alias_move_cases import Runner
from tests.world import DEP, registry_owned

HELD = ANNOTATION_PREFIX + "held"


def world() -> tuple[AliasWorld, Runner]:
    w = AliasWorld()
    w.build(CASES["setup"])
    return w, Runner(w, CASES["setup"])


def go(runner: Runner, *steps: dict) -> None:
    for step in steps:
        runner.before = runner.w.store.generation()
        runner.do(step)
        runner.expect(step)


def ack(agent: str, running: int = 0) -> dict:
    return {"do": "ack", "agent": agent, "running": running, "expect": {}}


START = {"do": "assess", "expect": {}}, {"do": "start", "expect": {"move": "holding"}}


def desired(w: AliasWorld) -> dict:
    answer = w.get(f"/v1/clusters/{CLUSTER}/desired", token="provisioner")
    assert answer.status_code == 200, answer.text
    return answer.json()


def agent_annotations(document: dict, deployment: str) -> dict:
    entry = next(d for d in document["deployments"] if d["deployment"] == deployment)
    return next(o for o in entry["objects"] if o["kind"] == "Agent")["document"]["metadata"][
        "annotations"
    ]


def test_an_agent_with_no_hold_has_no_held_annotation_and_the_desired_document_has_no_holds():
    world = registry_owned()
    answer = world.get("/v1/clusters/kubani/desired", token="provisioner")

    assert answer.status_code == 200, answer.text
    found = answer.json()
    assert found["holds"] == []
    assert HELD not in agent_annotations(found, DEP)
    status = world.get("/v1/status", cluster="kubani").json()
    assert status["aliasMoves"] == []
    assert [a["held"] for a in status["agents"]] == [None]


def test_a_report_without_holds_and_one_for_a_hold_that_does_not_exist_change_nothing():
    w, runner = world()
    before = w.store.generation()

    w.provisioner_pass()
    w.provisioner_pass(holds=[])
    w.provisioner_pass(holds=[{"move": "mv-nope", "deployment": dep_of("passes"), "running": 0}])

    assert w.store.generation() == before
    assert desired(w)["holds"] == []


def test_the_desired_document_lists_the_holds_still_to_confirm_and_the_agent_carries_the_move():
    w, runner = world()
    go(runner, {"do": "assess", "expect": {}}, {"do": "start", "sessions": "cancel", "expect": {}})

    found = desired(w)

    assert [(h["agent"], h["tenant"], h["namespace"], h["sessions"]) for h in found["holds"]] == [
        ("fails", "tenant-test", "tenant-test", "cancel"),
        ("no-suite", "tenant-test", "tenant-test", "cancel"),
        ("passes", "tenant-test", "tenant-test", "cancel"),
    ]
    assert {h["move"] for h in found["holds"]} == {w.move_id}
    assert agent_annotations(found, dep_of("passes"))[HELD] == w.move_id
    go(runner, ack("passes"))
    assert [h["agent"] for h in desired(w)["holds"]] == ["fails", "no-suite"]
    assert agent_annotations(desired(w), dep_of("passes"))[HELD] == w.move_id  # held, still held


def test_a_count_above_zero_is_stored_once_and_the_hold_waits():
    w, runner = world()
    go(runner, *START, ack("passes", 2))
    generation = w.store.generation()

    go(runner, ack("passes", 2))

    assert w.store.generation() == generation
    (hold,) = w.store.holds(move_id=w.move_id, deployment_id=dep_of("passes"))
    assert (hold.state, hold.running) == ("holding", 2)
    go(runner, ack("passes", 0))
    (hold,) = w.store.holds(move_id=w.move_id, deployment_id=dep_of("passes"))
    assert (hold.state, hold.running) == ("held", 0)


def test_an_entry_for_a_hold_that_was_released_is_ignored():
    w, runner = world()
    go(runner, *START, {"do": "cancel", "expect": {"move": "cancelled"}})
    generation = w.store.generation()

    go(runner, ack("passes"))

    assert w.store.generation() == generation
    assert {h.state for h in w.store.holds(move_id=w.move_id)} == {"released"}


def test_a_held_revision_says_why_it_waits_once():
    w, runner = world()
    go(
        runner,
        *START,
        ack("passes"),
        ack("fails"),
        ack("no-suite"),
        {"do": "feed", "pair": {"engine": "fast", "release": "r2"}, "expect": {"move": "moving"}},
        {"do": "ready", "agent": "passes", "expect": {"reason": {"passes": "held by alias move"}}},
    )
    reasons = [
        e for e in w.store.events(deployment_id=dep_of("passes")) if e.type == "held-by-alias-move"
    ]

    w.clock(30)
    w.clock(30)

    after = [
        e for e in w.store.events(deployment_id=dep_of("passes")) if e.type == "held-by-alias-move"
    ]
    assert len(reasons) == len(after) == 1
    assert w.newest("passes").reason == f"held by alias move {w.move_id}"
    assert w.newest("passes").phase == "awaiting-runtime"


def test_status_shows_the_open_moves_and_each_agents_hold():
    w, runner = world()
    go(runner, *START, ack("passes"))

    status = w.get("/v1/status", cluster=CLUSTER).json()

    held = {a["agent"]: a["held"] for a in status["agents"]}
    assert held["passes"] == {
        "move": w.move_id,
        "state": "held",
        "reason": f"held by alias move {w.move_id}",
    }
    assert held["fails"]["state"] == "holding"
    assert held["by-hand"] is None and held["off"] is None
    (move,) = status["aliasMoves"]
    assert move["id"] == w.move_id and move["state"] == "holding"
    assert move["next"] == "waiting for 2 agent(s) to stop: fails, no-suite"


def test_a_dependant_can_be_enabled_when_its_alias_has_no_open_move():
    w, runner = world()

    go(runner, {"do": "enable", "agent": "off", "expect": {}})

    assert w.store.deployment(dep_of("off")).enabled is True
