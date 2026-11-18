"""Milestone 4, the effect a skill's script needs, with no cluster.

A release whose agent lists a skill with a script asks for the effect `sandbox_execute`. The
tenant's Namespace grants it, or the release is never Ready: nobody can approve it, the status
says why, and the release before it keeps running. With the grant the release waits for a
person, who is shown what the effect means, and its sandbox is then told it holds the effect.
Withdrawing the grant takes the release out of service at the next reconcile: no session of it
can be started.
"""

from __future__ import annotations

from typing import Any

import yaml
from agent_platform.effects import ANNOTATION_GRANTED_EFFECTS
from loopworld.world import CLUSTER, TENANT, World
from test_milestone1 import AGENT, D
from test_milestone2 import approve, current_release, passes, ready

SKILL = {
    "name": "row-count",
    "digest": "sha256:" + "62" * 32,
    "description": "Counts rows in a table. Use when asked how many rows a table has.",
    "declared": True,
    "scripts": ["scripts/count.py"],
    "interpreters": ["python3"],
}
SENTENCE = (
    "effect sandbox_execute: skill scripts run inside the agent's container and can read the "
    "session's identity token"
)


def with_a_script(world: World, version: str, build: str) -> tuple[int, str]:
    """The release workflow reports a release of an agent that lists a skill with a script:
    its declaration asks for the effect and the release names the skill. Returns the
    revision and the release's name."""

    def asks(spec: dict) -> None:
        spec["capabilities"]["effects"] = ["sandbox_execute"]

    body = world.publish_declared(AGENT, version, build, change=asks)
    body["agentRelease"]["spec"]["skills"] = [SKILL]
    answer = world.report_release(body, version)
    assert answer.status_code == 200, answer.text
    number = answer.json()["revision"]
    return number, world.store.release(world.store.revision(D, number).release_id).name


def grant(world: World, effects: str | None) -> None:
    """A reviewed change to the tenant's Namespace: the annotation is set, or taken away."""
    namespace = world.cluster.get("v1", "Namespace", None, TENANT)
    annotations = namespace["metadata"].setdefault("annotations", {})
    if effects is None:
        annotations.pop(ANNOTATION_GRANTED_EFFECTS, None)
    else:
        annotations[ANNOTATION_GRANTED_EFFECTS] = effects
    world.cluster.put(namespace)


def condition(world: World, name: str) -> dict[str, Any]:
    found = (world.release(name) or {}).get("status", {}).get("conditions", [])
    return next(c for c in found if c["type"] == "Ready")


def template_environment(world: World, name: str) -> dict[str, str] | None:
    """What the release's sandbox is started with, or None when no pod template was made."""
    held = (world.release(name) or {}).get("status", {}).get("podTemplateConfigMap")
    if held is None:
        return None
    template = world.cluster.get("v1", "ConfigMap", TENANT, held)
    (document,) = template["data"].values()
    found: dict[str, str] = {}
    for container in yaml.safe_load(document)["spec"]["containers"]:
        found.update({e["name"]: e.get("value") for e in container.get("env") or []})
    return found


def preview(world: World, number: int) -> dict[str, Any]:
    return world.read(f"/v1/deployments/{D}/revisions/{number}/approval", token="operator")


def test_a_script_runs_only_where_the_tenant_grants_the_effect(world: World):
    current_release(world)
    old = world.agent_status(AGENT)["current"]["release"]

    # -- 1. no grant: the release is never Ready, and the reason is the grant ----------------
    number, new = with_a_script(world, "0.1.4", "b")
    passes(world, 4)
    found = condition(world, new)
    assert (found["status"], found["reason"]) == ("False", "EffectNotGranted")
    assert found["message"] == "effects not granted: sandbox_execute"
    assert world.release(new)["spec"]["skills"] == [SKILL]

    # -- 2. nobody can approve it, and the release before it keeps running -------------------
    seen = preview(world, number)
    assert seen["approvable"] is False
    assert seen["gateFailures"] == ["release is not waiting solely on approval"]
    status = world.agent_status(AGENT)
    assert status["blockers"] == [
        f"release {new}: release is not waiting solely on approval "
        "(EffectNotGranted: effects not granted: sandbox_execute)"
    ]
    assert (status["current"]["release"], ready(world, old)) == (old, "True")
    assert "AGENT_EFFECTS" not in template_environment(world, old)

    # -- 3. the tenant grants the effect: now it waits for a person, who sees what it means --
    grant(world, "sandbox_execute")
    passes(world, 2)
    found = condition(world, new)
    assert (found["status"], found["reason"], found["message"]) == ("False", "NotApproved", "")
    seen = preview(world, number)
    assert any(SENTENCE in change for change in seen["changes"]), seen["changes"]
    approve(world, number, "loop-m4-effect")
    passes(world, 4)

    # -- 4. Ready, and its sandbox is told it holds the effect ------------------------------
    assert ready(world, new) == "True"
    assert template_environment(world, new)["AGENT_EFFECTS"] == "sandbox_execute"
    assert "AGENT_EFFECTS" not in template_environment(world, old)
    passes(world, 3)
    assert world.agent_status(AGENT)["current"]["release"] == new

    # -- 5. the catalog knows the skill and who uses it -------------------------------------
    used = world.read("/v1/dependants", cluster=CLUSTER, type="skill", name=SKILL["name"])
    assert [(d["agent"], d["names"]) for d in used["dependants"]] == [(AGENT, [SKILL["digest"]])]

    # -- 6. the grant is withdrawn: out of service at the next reconcile ---------------------
    started = world.start_session(AGENT, "Count the rows of public.orders.")
    assert started.status_code == 202, started.text
    grant(world, None)
    world.reconcile()
    found = condition(world, new)
    assert (found["status"], found["reason"]) == ("False", "EffectNotGranted")
    assert ready(world, old) == "True"
    answer = world.start_session(AGENT, "Count the rows of public.orders again.")
    assert answer.status_code == 409, answer.text
    assert answer.json()["error"]["message"] == (
        f"release {new} is not Ready: EffectNotGranted: effects not granted: sandbox_execute"
    )
