"""Milestone 1, from start to end, with no cluster.

The registry learns what runs from the provisioner's reports, takes one agent over from the
pull request path without changing it, carries a new release of it from publication to
activation through a person's approval, and hands the agent back as it was.
"""

from __future__ import annotations

import copy

from agent_registry.config import ANNOTATION_PREFIX
from loopworld.cluster import AV
from loopworld.world import CLUSTER, PERSON, TENANT, World, agents, committed

AGENT = "db-admin"
D = f"agent:{CLUSTER}:{TENANT}:{AGENT}"
TOOL_SERVER = "postgres-admin-mcp"
TOOL_IMAGE = (
    "docker.io/crystaldba/postgres-mcp"
    "@sha256:dbbd346860d29f1543e991f30f3284bf4ab5f096d049ecc3426528f20b1b6e6b"
)


def platform_specs(world: World) -> dict[tuple, dict]:
    """The spec of every Agent and AgentRelease on the cluster."""
    return {
        key: copy.deepcopy(obj["spec"])
        for key, obj in world.cluster.objects.items()
        if key[0] == AV
    }


def registry_annotations(obj: dict) -> dict[str, str]:
    annotations = obj["metadata"].get("annotations") or {}
    return {k: v for k, v in annotations.items() if k.startswith(ANNOTATION_PREFIX)}


def results(report: list[dict]) -> dict[str, str]:
    return {entry["object"]: entry["result"] for entry in report}


def running_platform(world: World) -> None:
    """The cluster as it is before the registry exists: every agent delivered by Flux, the
    tool server db-admin uses running, the controller done with all of it."""
    assert world.apply_feed()
    for agent in agents():
        world.flux_applies(agent)
    world.workload("mcp", TOOL_SERVER, TOOL_IMAGE)
    assert world.settle() == []  # the registry owns nothing yet: nothing to hold


def handed_over(world: World) -> None:
    """db-admin leaves Flux's inventory with its objects in place, and a person takes it."""
    for document in (committed(AGENT)[0], *committed(AGENT)[1]):
        world.cluster.flux_release(document)
    world.settle()
    assert world.write(f"/v1/deployments/{D}/authority/take")["changed"] is True
    world.settle()


def test_the_registry_takes_an_agent_releases_it_and_gives_it_back(world: World):
    declared, releases = committed(AGENT)
    first, second = (r["metadata"]["name"] for r in releases)
    running_platform(world)

    # -- 1. the registry's view comes from the cluster, and agrees with it ------------------
    status = world.status()
    assert status["mode"] == "normal"
    assert [a["agent"] for a in status["agents"]] == agents()
    assert {a["authority"] for a in status["agents"]} == {"pull-request"}
    seen = world.agent_status(AGENT)
    assert (seen["currentRelease"], seen["phase"], seen["ready"]) == (second, "activated", True)
    assert seen["observation"]["state"] == "fresh"
    servers = {s["name"]: s for s in status["toolServers"]}
    assert sorted(n for n, s in servers.items() if not s["registered"]) == [
        "github-mcp",
        "graphiti-mcp",
        "postgres-mcp",
    ]
    assert servers[TOOL_SERVER]["observedImages"] == [TOOL_IMAGE]
    assert servers[TOOL_SERVER]["readyReplicas"] == 1

    # -- 2. who depends on a tool server ----------------------------------------------------
    answer = world.read("/v1/dependants", cluster=CLUSTER, type="tool", name="postgres-admin")
    assert answer["providedBy"] == f"tool-server:{CLUSTER}:-:{TOOL_SERVER}"
    assert [(d["agent"], d["release"], d["names"]) for d in answer["dependants"]] == [
        (AGENT, second, ["execute_sql"])
    ]

    # -- 3. a tool server the provisioner can no longer read is shown as not known ----------
    workload = ("apps/v1", "Deployment", "mcp", TOOL_SERVER)
    world.cluster.unreadable.add(workload)
    world.tick(200)
    world.settle()
    stale = {s["name"]: s for s in world.status()["toolServers"]}[TOOL_SERVER]
    assert stale["observation"]["state"] == "old"
    assert stale["observation"]["ageSeconds"] >= 200
    assert world.agent_status(AGENT)["observation"]["state"] == "fresh"
    world.cluster.unreadable.discard(workload)
    world.settle()
    fresh = {s["name"]: s for s in world.status()["toolServers"]}[TOOL_SERVER]
    assert fresh["observation"]["state"] == "fresh"

    # -- 4. taken while Flux still lists it: the provisioner writes nothing ------------------
    before = platform_specs(world)
    mark = len(world.cluster.writes)
    taken = world.write(f"/v1/deployments/{D}/authority/take")
    assert taken["changed"] is True
    assert taken["deployment"]["authority"] == "registry"
    held = world.settle()
    assert set(results(held).values()) == {"refused-flux-inventory"}
    assert world.provisioner_writes(mark) == []
    assert registry_annotations(world.agent(AGENT)) == {}

    # -- 5. Flux lets go; the registry adopts the objects and changes no spec ----------------
    for document in (declared, *releases):
        world.cluster.flux_release(document)
    adopted = world.settle()
    assert results(adopted) == {
        f"agent/{AGENT}": "applied",
        f"agentrelease/{first}": "applied",
        f"agentrelease/{second}": "applied",
    }
    assert platform_specs(world) == before
    assert world.cluster.deletes() == []
    assert registry_annotations(world.agent(AGENT)) == {
        ANNOTATION_PREFIX + "deployment": D,
        ANNOTATION_PREFIX + "cluster": CLUSTER,
        ANNOTATION_PREFIX + "generation": str(taken["deployment"]["generation"]),
        ANNOTATION_PREFIX + "current-release": second,
    }
    # The other twelve stay on the pull request path, untouched.
    assert {name for _, _, name in world.provisioner_writes(mark)} == {AGENT, first, second}
    for other in (a for a in agents() if a != AGENT):
        assert registry_annotations(world.agent(other)) == {}
    seen = world.agent_status(AGENT)
    assert (seen["authority"], seen["currentRelease"], seen["ready"]) == ("registry", second, True)
    assert set(results(world.settle()).values()) == {"unchanged"}

    # -- 6. a new release is published: on the cluster unapproved, then waiting for a person -
    published = world.publish(AGENT, "0.1.3", "a")
    new = published["agentRelease"]["metadata"]["name"]
    recorded = world.write(f"/v1/deployments/{D}/releases", published)
    assert (recorded["created"], recorded["revision"]["number"]) == (True, 3)
    assert world.write(f"/v1/deployments/{D}/releases", published)["created"] is False
    assert results(world.settle())[f"agentrelease/{new}"] == "applied"
    on_cluster = world.release(new)
    assert on_cluster["spec"] == {
        **published["agentRelease"]["spec"],
        "approval": {"approved": False},
    }
    world.settle()  # the controller's verdict on it reaches the registry
    ready = {c["type"]: c for c in world.release(new)["status"]["conditions"]}["Ready"]
    assert (ready["status"], ready["reason"]) == ("False", "NotApproved")
    seen = world.agent_status(AGENT)
    assert (seen["newestRelease"], seen["phase"]) == (new, "pending-approval")
    assert seen["currentRelease"] == second  # the running release is still the old one
    assert seen["nextActions"] == [f"just agent approve {AGENT}"]

    # -- 7. approval is a person's: preview, then approve what was previewed -----------------
    path = f"/v1/deployments/{D}/revisions/3/approval"
    assert world.call("GET", path, token="reader").status_code == 403
    preview = world.read(path, token="operator")
    assert preview["approvable"] is True
    assert (preview["gateFailures"], preview["changes"]) == ([], [])
    approval = {
        "approvalId": "loop-test-approval-1",
        "revision": 3,
        "authorityDigest": preview["authorityDigest"],
        "baseAuthorityDigest": preview["baseAuthorityDigest"],
        "policyRevision": preview["policyRevision"],
        "ticket": "LOOP-1",
    }
    for token in ("reader", "provisioner"):
        refused = world.call("POST", f"/v1/deployments/{D}/approvals", token=token, json=approval)
        assert refused.status_code == 403
    approved = world.write(f"/v1/deployments/{D}/approvals", approval)["approval"]
    assert approved["approvedBy"] == PERSON

    # -- 8. the approval reaches the cluster, the controller says Ready, the release is current
    assert results(world.settle())[f"agentrelease/{new}"] == "applied"
    assert world.release(new)["spec"]["approval"] == {
        "approved": True,
        "approvedBy": PERSON,
        "approvedAt": approved["approvedAt"],
        "ticket": "LOOP-1",
    }
    assert world.agent_status(AGENT)["currentRelease"] == second  # not before Ready is seen
    world.settle()
    seen = world.agent_status(AGENT)
    assert (seen["currentRelease"], seen["phase"], seen["ready"]) == (new, "activated", True)
    # The pointer moved in this pass; the cluster's Agent names the new release after the next.
    assert seen["acknowledged"] is False
    world.settle()
    named = registry_annotations(world.agent(AGENT))[ANNOTATION_PREFIX + "current-release"]
    assert named == new
    seen = world.agent_status(AGENT)
    assert (seen["blockers"], seen["nextActions"], seen["acknowledged"]) == ([], [], True)
    kept = platform_specs(world)
    assert {k: v for k, v in kept.items() if k[3] != new} == before
    events = world.read("/v1/events", deployment=D)["events"]
    assert [e["type"] for e in events] == [
        "agent-imported",
        "authority-taken",
        "release-recorded",
        "runtime-verdict",
        "approval-needed",
        "approved",
        "runtime-verdict",
        "revision-activated",
    ]

    # -- 9. given back: the documents for git are the ones git had, plus the new release -----
    mark = len(world.cluster.writes)
    back = world.write(f"/v1/deployments/{D}/authority/return")
    assert (back["changed"], back["deployment"]["authority"]) == (True, "pull-request")
    assert back["agent"] == world.cluster.defaulted(declared)  # what git had, defaults filled
    assert back["agent"]["spec"] == world.agent(AGENT)["spec"]
    assert back["agentReleases"][:2] == releases
    assert back["agentReleases"][2]["spec"] == world.release(new)["spec"]
    assert world.settle() == []
    assert world.provisioner_writes(mark) == []
    assert world.cluster.deletes() == []
    assert world.release(new) is not None

    # -- 10. and taken again, with nothing lost ----------------------------------------------
    again = world.write(f"/v1/deployments/{D}/authority/take")
    assert again["changed"] is True
    world.settle()
    assert set(results(world.settle()).values()) == {"unchanged"}
    assert world.agent_status(AGENT)["currentRelease"] == new
    assert world.cluster.deletes() == []


def test_a_release_that_is_not_signed_by_its_own_tag_cannot_be_approved(world: World):
    running_platform(world)
    handed_over(world)

    published = world.publish(AGENT, "0.1.3", "b")
    new = published["agentRelease"]["metadata"]["name"]
    # Signed, but by another agent's release tag.
    world.signatures.sign(published["agentRelease"]["spec"]["image"], "researcher", "v0.1.3")
    world.write(f"/v1/deployments/{D}/releases", published)
    world.settle()
    world.settle()

    seen = world.agent_status(AGENT)
    assert seen["phase"] == "awaiting-runtime"
    assert seen["nextActions"] == []
    preview = world.read(f"/v1/deployments/{D}/revisions/3/approval", token="operator")
    assert preview["approvable"] is False
    assert preview["gateFailures"]
    refused = world.call(
        "POST",
        f"/v1/deployments/{D}/approvals",
        json={
            "approvalId": "loop-test-approval-2",
            "revision": 3,
            "authorityDigest": preview["authorityDigest"],
            "baseAuthorityDigest": preview["baseAuthorityDigest"],
            "policyRevision": preview["policyRevision"],
        },
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "refused"
    assert world.release(new)["spec"]["approval"] == {"approved": False}
    assert world.agent_status(AGENT)["currentRelease"] == committed(AGENT)[1][1]["metadata"]["name"]


def test_a_restored_registry_activates_nothing_and_reports_what_it_missed(new_world):
    # Two registries with the same history up to the dump.
    live, restored = new_world(), new_world()
    for world in (live, restored):
        running_platform(world)
        handed_over(world)
    assert live.status()["generation"] == restored.status()["generation"]

    # After the dump, the live registry disables the agent, and the cluster follows.
    live.write(f"/v1/deployments/{D}/enabled", {"enabled": False, "reason": "incident"})
    live.settle()
    assert live.agent(AGENT)["spec"]["enabled"] is False

    # The dump is restored and meets the cluster that kept running.
    restored.serve(live)
    before = platform_specs(restored)
    mark = len(restored.cluster.writes)
    assert restored.settle() == []
    assert restored.status()["mode"] == "recovery"
    assert restored.provisioner_writes(mark) == []
    assert platform_specs(restored) == before  # still disabled: nothing was turned back on

    refused = restored.call("POST", f"/v1/deployments/{D}/enabled", json={"enabled": True})
    assert refused.status_code != 200

    report = restored.read("/v1/recovery")
    assert (report["mode"], report["complete"], report["waitingFor"]) == ("recovery", True, [])
    differences = {(d["subject"], d["field"]): d for d in report["differences"]}
    missed = differences[(f"agent/{AGENT}", "enabled")]
    assert (missed["registry"], missed["cluster"]) == (True, False)

    # A person reads the differences and leaves recovery with exactly those.
    wrong = restored.call("POST", "/v1/recovery/leave", json={"digest": "0" * 64})
    assert wrong.status_code != 200
    left = restored.write("/v1/recovery/leave", {"digest": report["digest"]})
    assert left["mode"] == "normal"
    assert restored.agent_status(AGENT)["enabled"] is False
    restored.settle()
    assert set(results(restored.settle()).values()) == {"unchanged"}
    assert restored.agent(AGENT)["spec"]["enabled"] is False
    assert restored.cluster.deletes() == []
