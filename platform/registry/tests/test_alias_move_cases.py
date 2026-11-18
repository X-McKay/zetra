"""The scenarios of `contract/alias-move-cases.json`, step by step, through the HTTP
application. Every action and every `expect` key is defined in the file's `about`.

The runner works on the memory store always, and on PostgreSQL too when `REGISTRY_TEST_DSN` is
set. Each scenario is one test, named by the scenario."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

import pytest

from agent_registry.store import Store
from agent_registry.store.memory import MemoryStore
from tests.alias_world import CASES, CLUSTER, AliasWorld, Down, dep_of
from tests.test_store_pg import BASE_DSN, isolated_schema

# The events a step's `events` key is about: those named in section 6.2.
SECTION_6_2 = {
    "alias-move-started",
    "alias-move-held",
    "alias-move-cancelled",
    "alias-moved",
    "alias-moved-before-hold-confirmed",
    "alias-changed-without-move",
    "alias-move-evaluating",
    "hold-resumed",
    "hold-suspended",
    "alias-move-completed",
}
VERDICTS = {"pass": "passed", "fail": "failed"}


@contextmanager
def memory_store() -> Iterator[Callable[[Callable], Store]]:
    yield lambda clock: MemoryStore(clock=clock)


@contextmanager
def pg_store() -> Iterator[Callable[[Callable], Store]]:
    from agent_registry.store.pg import PgStore, migrate

    with isolated_schema() as dsn, ExitStack() as stack:
        migrate(dsn)

        def make(clock: Callable) -> Store:
            store = PgStore(dsn, clock=clock)
            stack.callback(store.close)
            return store

        yield make


STORES = [
    pytest.param(memory_store, id="memory"),
    pytest.param(
        pg_store,
        id="postgres",
        marks=[
            pytest.mark.postgres,
            pytest.mark.skipif(not BASE_DSN, reason="REGISTRY_TEST_DSN is not set"),
        ],
    ),
]


class Runner:
    def __init__(self, world: AliasWorld, setup: dict[str, Any]):
        self.w = world
        self.setup = setup
        self.answer = None
        self.before = 0

    def candidate(self, step: dict[str, Any]) -> dict[str, str]:
        return step.get("candidate") or self.setup["candidate"]

    def body_of(self, step: dict[str, Any]) -> dict[str, Any]:
        return {"cluster": self.setup["cluster"], "alias": self.setup["alias"]} | {
            "candidate": self.candidate(step)
        }

    # -- actions -----------------------------------------------------------------------------

    def do(self, step: dict[str, Any]) -> None:
        action = step["do"].replace("-", "_")
        getattr(self, action)(step)

    def assess(self, step):
        self.answer = self.w.post("/v1/assessments", self.body_of(step))
        if self.answer.status_code == 200:
            self.w.assessment = self.answer.json()

    def start(self, step):
        body = {
            "assessment": self.w.assessment["id"],
            "sessions": step.get("sessions", "finish"),
            "evaluationSeconds": step.get("evaluationSeconds", 600),
        }
        self.answer = self.w.post("/v1/alias-moves", body)
        if self.answer.status_code == 200:
            self.w.move_id = self.answer.json()["id"]

    def ack(self, step):
        hold = {
            "move": self.w.move_id,
            "deployment": dep_of(step["agent"]),
            "running": step["running"],
        }
        self.answer = self.w.provisioner_pass(holds=[hold])

    def feed(self, step):
        self.w.apply_pair(step["pair"])
        self.answer = None

    def ready(self, step):
        """The cluster reports the newest revision of the agent approved and Ready."""
        self.w.reporting.add(step["agent"])
        self.w.tick()
        self.w.settle()
        self.answer = None

    def engine(self, step):
        self.w.put_engine(step["readyReplicas"], step["servedName"])
        self.answer = None

    def evaluate(self, step):
        self.answer = self.w.post(f"/v1/alias-moves/{self.w.move_id}/evaluate")

    def result(self, step):
        """The evaluator claims the agent's open evaluation and records a verdict."""
        want = dep_of(step["agent"])
        # Claiming hands out the oldest evaluation of the cluster, so an earlier claim for
        # another agent may already have taken this one.
        mine = [e for e in self.w.store.evaluations(want) if e.state == "claimed"]
        for _ in range(20):
            if mine:
                claimed = {
                    "deployment": want,
                    "revision": mine[0].revision,
                    "attempt": mine[0].attempt,
                }
                break
            claimed = self.w.post(
                f"/v1/clusters/{CLUSTER}/evaluations/claim",
                {"evaluator": "someone-else", "leaseSeconds": 3600},
                token="evaluator",
            ).json()["evaluation"]
            assert claimed is not None, f"{step['agent']} has no evaluation to claim"
            if claimed["deployment"] == want:
                break
        else:
            raise AssertionError("no claim for the agent")
        body = {
            "evaluator": "someone-else",
            "evaluatorVersion": "0.1.0",
            "verdict": VERDICTS[step["verdict"]],
            "summary": "from the case table",
            "cases": [],
        }
        self.answer = self.w.post(
            f"/v1/deployments/{want}/revisions/{claimed['revision']}/evaluations/"
            f"{claimed['attempt']}/result",
            body,
            token="evaluator",
        )

    def clock(self, step):
        self.w.clock(step["seconds"])
        self.answer = None

    def cancel(self, step):
        self.answer = self.w.post(f"/v1/alias-moves/{self.w.move_id}/cancel", {"reason": "case"})

    def check(self, step):
        self.answer = self.w.post("/v1/checks/alias", self.body_of(step), token="reader")

    def enable(self, step):
        self.answer = self.w.post(
            f"/v1/deployments/{dep_of(step['agent'])}/enabled", {"enabled": True}
        )

    def disable(self, step):
        self.answer = self.w.post(
            f"/v1/deployments/{dep_of(step['agent'])}/enabled", {"enabled": False}
        )

    def evaluate_again(self, step):
        number = self.w.newest(step["agent"]).number
        self.answer = self.w.post(
            f"/v1/deployments/{dep_of(step['agent'])}/revisions/{number}/evaluations",
            {"reason": "case"},
        )

    def new_dependant(self, step):
        """Another registry-owned agent that uses the alias is activated."""
        self.w.add_agent(
            {
                "agent": "newcomer",
                "authority": "registry",
                "enabled": True,
                "suite": True,
                "tools": False,
            }
        )
        self.answer = None

    def database_down(self, step):
        self.w.app.state.store = Down()
        self.answer = None

    # -- what must hold afterwards -----------------------------------------------------------

    def expect(self, step: dict[str, Any]) -> None:
        expected = step["expect"]
        where = f"after {step['do']} {step.get('agent', '')}".strip()
        if "error" in expected:
            assert self.answer is not None and self.answer.status_code >= 400, where
            assert self.answer.json()["error"]["code"] == expected["error"], where
            return
        if self.answer is not None:
            assert self.answer.status_code == 200, f"{where}: {self.answer.text}"
        w = self.w
        store = w.app.state.store
        if "move" in expected:
            assert store.alias_move(w.move_id).state == expected["move"], where
        for agent, state in expected.get("holds", {}).items():
            (hold,) = store.holds(move_id=w.move_id, deployment_id=dep_of(agent))
            assert hold.state == state, f"{where}: hold of {agent}"
        if "held" in expected:
            assert w.held_agents() == set(expected["held"]), where
        if "dependants" in expected:
            found = {d["agent"]: d["held"] for d in self.answer.json()["dependants"]}
            assert found == expected["dependants"], where
        if "blocked" in expected:
            assert self.answer.json()["blocked"] is expected["blocked"], where
        if "allowed" in expected:
            assert self.answer.json()["allowed"] is expected["allowed"], where
        if "events" in expected:
            written = [e.type for e in store.events(after=self.before, limit=1000)]
            assert [t for t in written if t in SECTION_6_2] == expected["events"], where
        for agent, phase in expected.get("phase", {}).items():
            assert w.newest(agent).phase == phase, f"{where}: phase of {agent}"
        for agent, text in expected.get("reason", {}).items():
            assert text in w.newest(agent).reason, f"{where}: reason of {agent}"


def run(make_store, scenario: dict[str, Any]) -> None:
    with make_store() as make:
        world = AliasWorld(make)
        world.build(CASES["setup"])
        runner = Runner(world, CASES["setup"])
        for position, step in enumerate(scenario["steps"], start=1):
            runner.before = world.store.generation()
            runner.do(step)
            try:
                runner.expect(step)
            except AssertionError as error:
                raise AssertionError(f"step {position} ({step['do']}): {error}") from None


def name_of(scenario: dict[str, Any]) -> str:
    return "test_" + re.sub(r"[^a-z0-9]+", "_", scenario["name"].lower()).strip("_")


def _scenario_test(scenario: dict[str, Any]) -> Callable:
    @pytest.mark.parametrize("make_store", STORES)
    def test(make_store):
        run(make_store, scenario)

    test.__name__ = test.__qualname__ = name_of(scenario)
    test.__doc__ = scenario["name"]
    return test


for _scenario in CASES["scenarios"]:
    globals()[name_of(_scenario)] = _scenario_test(_scenario)
