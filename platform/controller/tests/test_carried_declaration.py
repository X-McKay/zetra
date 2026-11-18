"""Delivery contract section 2 and 9: releases carry their declaration; the rate limit follows
the Agent's current release."""

from __future__ import annotations

import copy

import yaml

from tests.conftest import AGENT, make_release

GV = "agentplatform.io/v1alpha1"
CURRENT = "registry.agentplatform.io/current-release"


def declared(agent: dict, tools: list[str] | None = None, tokens: int | None = None) -> dict:
    """The Agent as a release was built (optionally with other tools or budget), and its
    declaration."""
    built = copy.deepcopy(agent)
    mcp = built["spec"]["capabilities"]["mcp"][0]
    if tools is not None:
        mcp["tools"] = tools
    if tokens is not None:
        built["spec"]["capabilities"]["models"][0]["budget"]["tokensPerDay"] = tokens
    declaration = {k: v for k, v in built["spec"].items() if k != "enabled"}
    return built, declaration


def add_release(world, name, tools=None, tokens=None, declaration=True, **spec):
    built, decl = declared(AGENT, tools, tokens)
    if declaration:
        spec["declaration"] = decl
    world.add(make_release(built, world.cfg, name=name, **spec))


def ratelimit(world) -> dict:
    cm = world.get("v1", "ConfigMap", "agent-platform", "ratelimit-config")
    return {
        d["value"]: d["rate_limit"]["requests_per_unit"]
        for d in yaml.safe_load(next(iter(cm["data"].values())))["descriptors"]
        if "value" in d
    }


def live_agent(world, **annotations):
    agent = copy.deepcopy(AGENT)
    agent["metadata"]["annotations"] = annotations
    world.add(agent)


def test_two_releases_with_differing_declarations_are_both_ready(world):
    world.add(AGENT)
    add_release(world, "investigator-r1")
    add_release(world, "investigator-r2", tools=["pods_get"])
    world.reconcile_agent()
    for name in ("investigator-r1", "investigator-r2"):
        status = world.release(name)["status"]
        assert status["policyDrift"] is False and status["ready"] is True


def test_declaration_that_does_not_compile_to_the_digest_is_drift(world):
    world.add(AGENT)
    add_release(world, "investigator-r1")
    release = world.kube.obj(GV, "AgentRelease", "tenant-a", "investigator-r1")
    release["spec"]["declaration"]["capabilities"]["mcp"][0]["tools"].append("secrets_get")
    world.reconcile_agent()
    status = world.release()["status"]
    assert status["policyDrift"] is True and status["ready"] is False


def test_release_without_declaration_still_drifts_with_the_live_agent(world):
    world.add(AGENT)
    add_release(world, "investigator-r1", declaration=False)
    world.reconcile_agent()
    assert world.release()["status"]["ready"] is True
    world.kube.obj(GV, "Agent", "tenant-a", "investigator")["spec"]["capabilities"]["mcp"][0][
        "tools"
    ].append("secrets_get")
    world.reconcile_agent()
    assert world.release()["status"]["policyDrift"] is True


def test_release_with_declaration_does_not_drift_when_the_live_agent_changes(world):
    world.add(AGENT)
    add_release(world, "investigator-r1")
    world.reconcile_agent()
    before = world.release()["status"]
    world.kube.obj(GV, "Agent", "tenant-a", "investigator")["spec"]["capabilities"]["mcp"][0][
        "tools"
    ].append("secrets_get")
    world.reconcile_agent()
    after = world.release()["status"]
    assert after["policyDrift"] is False and after["ready"] is True
    assert after["policyDigest"] == before["policyDigest"]


def test_disabled_live_agent_holds_a_release_with_declaration(world):
    world.add(AGENT)
    add_release(world, "investigator-r1")
    world.reconcile_agent()
    world.kube.obj(GV, "Agent", "tenant-a", "investigator")["spec"]["enabled"] = False
    world.reconcile_agent()
    status = world.release()["status"]
    assert status["ready"] is False and status["policyDrift"] is False
    reasons = {c["type"]: c["reason"] for c in status["conditions"]}
    assert reasons["Ready"] == "AgentDisabled"


def test_ratelimit_follows_the_current_release(world):
    live_agent(world, **{CURRENT: "investigator-r1"})
    add_release(world, "investigator-r1", tokens=1000)
    add_release(world, "investigator-r2", tokens=2000)
    world.reconcile_agent()
    assert ratelimit(world) == {"tenant-a/investigator": 1000}


def test_ratelimit_without_annotation_is_as_before(world):
    live_agent(world)
    add_release(world, "investigator-r1", tokens=1000)
    add_release(world, "investigator-r2", tokens=2000)
    world.reconcile_agent()
    # Without a current release the last ready release in listing order wins, as today.
    assert ratelimit(world) == {"tenant-a/investigator": 2000}


def test_ratelimit_with_current_release_not_ready_is_as_before(world):
    live_agent(world, **{CURRENT: "investigator-r1"})
    add_release(world, "investigator-r1", tokens=1000, approval={"approved": False})
    add_release(world, "investigator-r2", tokens=2000)
    world.reconcile_agent()
    assert world.release("investigator-r1")["status"]["ready"] is False
    assert ratelimit(world) == {"tenant-a/investigator": 2000}
