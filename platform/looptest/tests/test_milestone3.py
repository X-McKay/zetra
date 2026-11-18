"""Milestone 3, a dependency that changes under an agent, with no cluster.

The agent is db-admin as milestone 2 leaves it: taken over, released by its workflow, evaluated
and current. A proposed tool contract that drops a tool it uses is refused by name. An alias is
moved to another model release: the agent is held, its running session is waited for, the alias
moves, the engine serves, the agent is evaluated on the new model and resumed. And the two ways
a move ends badly: the agent fails on the new model and stays held, and the engine never serves
and the alias is moved back.
"""

from __future__ import annotations

from typing import Any

from agent_registry.config import ANNOTATION_PREFIX
from loopworld.sessions import COMPLETED
from loopworld.world import CLUSTER, World
from test_milestone1 import AGENT, TOOL_IMAGE, TOOL_SERVER, D
from test_milestone2 import (
    BAD,
    GOOD,
    REQUEST,
    current_release,
    deliver,
    passes,
    refused,
    registry_events,
)

ALIAS, ENGINE = "main", "main"
OLD, NEW = "Qwen3.6-35B-A3B-NVFP4", "Qwen3.7-NEXT"
SERVED = {"r1": OLD, "r2": NEW}
FROM, CANDIDATE = ({"engine": ENGINE, "release": r} for r in ("r1", "r2"))
HELD = ANNOTATION_PREFIX + "held"
CONFIG = "sha256:" + "0" * 63 + "1"


def manifest(name: str) -> dict[str, Any]:
    """A model release manifest as the cluster repository's feed carries it."""
    return {
        "schemaVersion": 1,
        "engine": ENGINE,
        "name": name,
        "servedName": SERVED[name],
        "weights": {"source": f"hf://{SERVED[name]}", "revision": None, "files": None},
        "format": "safetensors",
        "quantisation": None,
        "engineImage": "vllm/vllm-openai:v0.30.0-aarch64",
        "serving": {"chatTemplate": None, "toolCallParser": None, "contextLength": None},
        "offers": {"toolCalling": True},
        "evidence": {"benchmark": "a.json", "changeRecord": "b.md"},
    }


def tool(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"{name}.",
        "effect": "remote_read",
        "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}},
        "outputSchema": None,
    }


def contract(*tools: str) -> dict[str, Any]:
    """The contract captured from db-admin's tool server."""
    return {
        "schemaVersion": 1,
        "server": TOOL_SERVER,
        "backend": "postgres-admin",
        "image": TOOL_IMAGE,
        "configDigest": CONFIG,
        "tools": [tool(name) for name in tools],
    }


def feed_names(world: World, release: str) -> None:
    """The feed says the engine serves this release."""
    engine = next(e for e in world.feed_document["modelEngines"] if e["name"] == ENGINE)
    engine["release"], engine["servedName"] = release, SERVED[release]


def current(world: World) -> int:
    return world.agent_status(AGENT)["current"]["revision"]


def dependencies_named(world: World) -> int:
    """Milestone 3 reaches a platform that milestone 2 left running: the feed gains the model
    release manifests, the release the engine serves, and the tool server's contract. The agent
    gets one new revision for it, which is evaluated and made current. Returns its number."""
    current_release(world)
    number = deliver(world, "0.1.4", "b")
    assert world.evaluate() is True
    passes(world, 2)
    assert current(world) == number

    document = world.feed_document
    document["modelReleases"] = [manifest("r1"), manifest("r2")]
    feed_names(world, "r1")
    server = next(s for s in document["toolServers"] if s["name"] == TOOL_SERVER)
    server["configDigest"] = CONFIG
    server["contract"] = contract("execute_sql", "list_schemas")
    world.engine_serves(ENGINE, OLD)
    assert world.apply_feed() is True

    newer = number + 1
    passes(world, 3)
    assert world.agent_status(AGENT)["candidate"]["revision"] == newer
    assert world.evaluate() is True
    passes(world, 2)
    assert current(world) == newer
    assert world.apply_feed() is False  # the same feed again changes nothing
    return newer


def assess(world: World, candidate: dict[str, str] = CANDIDATE) -> dict[str, Any]:
    return world.write(
        "/v1/assessments", {"cluster": CLUSTER, "alias": ALIAS, "candidate": candidate}
    )


def check(world: World, candidate: dict[str, str]) -> dict[str, Any]:
    """What the gate of the cluster repository asks before a change to an alias merges."""
    answer = world.call(
        "POST",
        "/v1/checks/alias",
        token="reader",
        json={"cluster": CLUSTER, "alias": ALIAS, "candidate": candidate},
    )
    assert answer.status_code == 200, answer.text
    return answer.json()


def move_of(world: World, move: str) -> dict[str, Any]:
    return world.read(f"/v1/alias-moves/{move}")


def hold_of(world: World, move: str) -> dict[str, Any]:
    (hold,) = move_of(world, move)["holds"]
    return hold


def held_on_cluster(world: World) -> str | None:
    return (world.agent(AGENT)["metadata"].get("annotations") or {}).get(HELD)


def held(world: World) -> str:
    """An assessment, a move started for it, and the agent stopped: the move is `held`."""
    found = assess(world)
    move = world.write("/v1/alias-moves", {"assessment": found["id"], "sessions": "finish"})["id"]
    passes(world, 2)
    assert move_of(world, move)["state"] == "held"
    return move


def moving(world: World) -> str:
    """A held move whose change has merged: the feed now says the engine serves the candidate."""
    move = held(world)
    assert check(world, CANDIDATE)["allowed"] is True
    feed_names(world, "r2")
    assert world.apply_feed() is True
    assert move_of(world, move)["state"] == "moving"
    return move


def test_a_contract_that_drops_a_tool_an_agent_uses_is_refused_by_name(world: World):
    dependencies_named(world)

    def ask(proposed: dict[str, Any], *, token: str = "reader"):
        return world.call(
            "POST",
            "/v1/checks/tool-contract",
            token=token,
            json={"cluster": CLUSTER, "server": TOOL_SERVER, "contract": proposed},
        )

    # -- 1. the contract the registry already holds, and one that drops a tool nobody uses ---
    for proposed in (contract("execute_sql", "list_schemas"), contract("execute_sql")):
        answer = ask(proposed).json()
        assert (answer["blocked"], answer["findings"]) == (False, [])

    # -- 2. the tool db-admin was released with is gone: blocked, naming the agent ------------
    answer = ask(contract("list_schemas")).json()
    assert answer["blocked"] is True
    assert [(f["agent"], f["tool"], f["finding"]) for f in answer["findings"]] == [
        (AGENT, "execute_sql", "tool-removed")
    ]
    assert answer["server"] == TOOL_SERVER
    assert (answer["backend"], answer["users"]) == ("postgres-admin", 1)

    # -- 3. asking changed nothing, and the provisioner's credential cannot ask --------------
    mark = len(registry_events(world))
    ask(contract("list_schemas"))
    assert len(registry_events(world)) == mark
    assert ask(contract("list_schemas"), token="provisioner").status_code == 403


def test_an_alias_is_moved_and_the_agent_is_evaluated_on_the_new_model(world: World):
    before = dependencies_named(world)
    release = world.agent_status(AGENT)["current"]["release"]

    # -- 1. the assessment names the agent; no change to the alias may merge yet --------------
    found = assess(world)
    mine = next(d for d in found["dependants"] if d["agent"] == AGENT)
    assert (mine["authority"], mine["held"], mine["evaluation"]) == ("registry", True, "required")
    assert mine["needs"] == [{"need": "toolCalling", "met": True}]
    others = [d for d in found["dependants"] if d["agent"] != AGENT]
    assert others and not any(d["held"] for d in others)  # the pull request path's agents
    assert (found["from"], found["candidate"], found["blocked"]) == (FROM, CANDIDATE, False)
    assert check(world, CANDIDATE)["allowed"] is False

    # -- 2. a move is started while a person's session runs: the agent is held, and waits -----
    assert world.start_session(AGENT, "long task").status_code == 202
    (long_task,) = world.temporal.running()
    move = world.write("/v1/alias-moves", {"assessment": found["id"], "sessions": "finish"})["id"]
    assert move_of(world, move)["state"] == "holding"
    passes(world)
    assert held_on_cluster(world) == move
    assert (hold_of(world, move)["state"], hold_of(world, move)["running"]) == ("holding", 1)
    assert world.temporal.cancelled == []
    refusal = world.start_session(AGENT, "another task")
    assert refusal.status_code == 409 and move in refusal.text
    said = check(world, CANDIDATE)
    assert said["allowed"] is False and AGENT in said["reason"]
    assert world.agent_status(AGENT)["held"]["move"] == move

    # -- 3. the session ends; the next pass counts none, and the change may merge ------------
    world.temporal.finish(long_task, GOOD)
    passes(world)
    assert move_of(world, move)["state"] == "held"
    assert check(world, CANDIDATE) == {
        "allowed": True,
        "move": move,
        "assessment": found["id"],
        "reason": "",
    }

    # -- 4. the change merges: the alias has moved, and the agent's new revision waits --------
    feed_names(world, "r2")
    assert world.apply_feed() is True
    assert move_of(world, move)["state"] == "moving"
    newer = before + 1
    passes(world, 4)
    waiting = world.agent_status(AGENT)
    assert (waiting["current"]["revision"], waiting["candidate"]["revision"]) == (before, newer)
    assert waiting["candidate"]["phase"] == "awaiting-runtime"
    assert move in waiting["candidate"]["reason"]
    assert world.evaluate() is False  # nothing is evaluated against an engine being swapped

    # -- 5. the engine has to be seen serving the candidate before anything is evaluated ------
    refused(world.call("POST", f"/v1/alias-moves/{move}/evaluate"), "refused", NEW)
    world.engine_serves(ENGINE, NEW)
    passes(world)
    world.write(f"/v1/alias-moves/{move}/evaluate")
    assert move_of(world, move)["state"] == "evaluating"

    # -- 6. the evaluator runs the suite on the held agent; a person still cannot ------------
    passes(world, 2)
    assert world.agent_status(AGENT)["candidate"]["phase"] == "evaluating"
    assert world.start_session(AGENT, "another task").status_code == 409
    assert world.evaluate() is True
    assert world.temporal.started()[-1]["purpose"] == "evaluation"

    # -- 7. it passed: the revision is current, the hold is gone, the move is complete --------
    passes(world, 2)
    seen = world.agent_status(AGENT)
    assert (seen["current"]["revision"], seen["current"]["release"]) == (newer, release)
    assert (seen["candidate"], seen["held"]) == (None, None)
    assert held_on_cluster(world) is None
    done = move_of(world, move)
    assert (done["state"], [h["state"] for h in done["holds"]]) == ("completed", ["resumed"])
    assert world.start_session(AGENT, "another task").status_code == 202
    assert world.read("/v1/alias-moves", cluster=CLUSTER, alias=ALIAS, open="true")["moves"] == []


def test_an_agent_that_fails_on_the_new_model_stays_held_until_it_passes(world: World):
    before = dependencies_named(world)
    move = moving(world)
    world.engine_serves(ENGINE, NEW)
    passes(world)
    world.write(f"/v1/alias-moves/{move}/evaluate")
    passes(world, 2)

    # -- 1. the suite fails on the new model: the move ends, the agent stays held -------------
    world.temporal.end(REQUEST, COMPLETED, BAD)
    assert world.evaluate() is True
    passes(world, 2)
    done = move_of(world, move)
    assert (done["state"], [h["state"] for h in done["holds"]]) == ("completed", ["suspended"])
    seen = world.agent_status(AGENT)
    assert seen["current"]["revision"] == before
    assert (seen["held"]["move"], seen["held"]["state"]) == (move, "suspended")
    assert "evaluation failed" in seen["held"]["reason"]
    assert held_on_cluster(world) == move
    assert world.start_session(AGENT, "a task").status_code == 409

    # -- 2. an operator asks for another evaluation; it passes, and the agent is resumed ------
    world.temporal.end(REQUEST, COMPLETED, GOOD)
    world.write(f"/v1/deployments/{D}/revisions/{before + 1}/evaluations", {"reason": "retry"})
    assert world.evaluate() is True
    passes(world, 2)
    seen = world.agent_status(AGENT)
    assert (seen["current"]["revision"], seen["held"]) == (before + 1, None)
    assert held_on_cluster(world) is None
    assert world.start_session(AGENT, "a task").status_code == 202


def test_a_move_whose_engine_never_serves_is_backed_out(world: World):
    before = dependencies_named(world)
    move = moving(world)
    passes(world, 2)

    # -- 1. the engine never serves the candidate: nothing can be evaluated or cancelled ------
    refused(world.call("POST", f"/v1/alias-moves/{move}/evaluate"), "refused", NEW)
    refused(
        world.call("POST", f"/v1/alias-moves/{move}/cancel", json={"reason": "no engine"}),
        "refused",
        "moving",
    )
    assert held_on_cluster(world) == move

    # -- 2. the way back is a change to the pair the alias came from, which may merge ---------
    assert check(world, FROM)["allowed"] is True
    feed_names(world, "r1")
    assert world.apply_feed() is True
    done = move_of(world, move)
    assert (done["state"], [h["state"] for h in done["holds"]]) == ("cancelled", ["released"])
    assert "moved back" in done["next"]

    # -- 3. the agent takes sessions again at once, on the revision it had -------------------
    passes(world, 2)
    assert held_on_cluster(world) is None
    assert world.agent_status(AGENT)["held"] is None
    assert current(world) == before
    assert world.start_session(AGENT, "a task").status_code == 202

    # -- 4. the revision for the candidate was never evaluated; the one for the pair the alias
    #       came back to is evaluated as after any change to an alias (milestone 2) -----------
    assert world.store.revision(D, before + 1).phase == "superseded"
    assert world.read(f"/v1/deployments/{D}/revisions/{before + 1}")["evaluations"] == []
    assert world.evaluate() is True
    passes(world, 2)
    assert current(world) == before + 2
