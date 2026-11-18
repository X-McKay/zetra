import copy
import json
from pathlib import Path

import pytest

from agent_registry import derive
from agent_registry.models import (
    Cluster,
    Deployment,
    Item,
    Release,
    Requirement,
    Revision,
    deployment_id,
    release_id,
    revision_id,
)
from agent_registry.rules import Refusal
from agent_registry.rules.impact import tool_contract
from agent_registry.store.memory import MemoryStore
from tests.world import CLUSTER, T0, TENANT

TABLE = json.loads((Path(__file__).parent.parent / "contract" / "impact-cases.json").read_text())
SERVER, BACKEND = "postgres-admin-mcp", "postgres-admin"
SERVER_DEP = deployment_id("tool-server", CLUSTER, None, SERVER)


def proposed_by(recorded: dict, mutations: list[dict]) -> dict:
    """The case's proposed contract: the recorded one with its mutations applied in order."""
    contract = copy.deepcopy(recorded)
    for step in mutations:
        tools = contract["tools"]
        if step["op"] == "remove-tool":
            contract["tools"] = [t for t in tools if t["name"] != step["tool"]]
        elif step["op"] == "add-tool":
            tools.append(copy.deepcopy(step["value"]))
        else:
            next(t for t in tools if t["name"] == step["tool"])[step["field"]] = copy.deepcopy(
                step["value"]
            )
    return contract


def _add_release(tx, agent: str, name: str, tools: list[str]) -> Release:
    release = Release(
        id=release_id("agent", agent, name),
        kind="agent",
        item=agent,
        name=name,
        digest="",
        origin="built",
        recorded_at=T0,
    )
    tx.add_release(release)
    tx.add_requirements(
        release.id,
        [Requirement(release_id=release.id, type="tool", target=BACKEND, names=sorted(tools))],
    )
    return release


def add_agent(
    tx, agent: str, authority: str, current: list[str], newest: dict | None, backend=BACKEND
) -> None:
    """An agent whose current release requires `current`, and a newer revision in the phase
    `newest` says. A user of another backend is `backend`."""
    dep = deployment_id("agent", CLUSTER, TENANT, agent)
    tx.put_item(Item(kind="agent", name=agent))
    tx.put_deployment(
        Deployment(
            id=dep,
            kind="agent",
            item=agent,
            cluster=CLUSTER,
            tenant=TENANT,
            namespace=TENANT,
            authority=authority,
            current_revision=1,
        )
    )
    releases = [(current, "activated")] + ([(newest["tools"], newest["phase"])] if newest else [])
    for number, (tools, phase) in enumerate(releases, start=1):
        release = _add_release(tx, agent, f"{agent}-{number}", tools)
        tx.add_revision(
            Revision(
                id=revision_id(dep, number),
                deployment_id=dep,
                number=number,
                release_id=release.id,
                origin="registry",
                created_at=T0,
                phase=phase,
            )
        )


def build(recorded: dict | None, *, with_backend: bool = True) -> MemoryStore:
    """The users of the table, and the tool server with `recorded` as its stored contract."""
    store = MemoryStore(clock=lambda: T0)
    with store.transaction("test") as tx:
        tx.put_cluster(Cluster(name=CLUSTER, trust_domain="t", gateway="g"))
        tx.put_item(Item(kind="tool-server", name=SERVER))
        tx.put_deployment(
            Deployment(
                id=SERVER_DEP,
                kind="tool-server",
                item=SERVER,
                cluster=CLUSTER,
                namespace="mcp",
                document={
                    "registered": with_backend,
                    "backend": BACKEND if with_backend else None,
                    "contract": derive.contract_record(recorded),
                },
            )
        )
        for user in TABLE["users"]:
            add_agent(tx, user["agent"], user["authority"], user["current"], user["newest"])
        tx.event("test")
    return store


@pytest.mark.parametrize("case", TABLE["cases"], ids=lambda c: c["name"])
def test_impact_case(case):
    stored = case["recorded"] if "recorded" in case else TABLE["recorded"]
    proposed = proposed_by(TABLE["recorded"], case["mutations"])

    answer = tool_contract(build(stored), CLUSTER, SERVER, proposed)

    assert [(f.agent, f.tool, f.finding) for f in answer.findings] == [
        tuple(f) for f in case["findings"]
    ]
    assert answer.blocked is case["blocked"]
    assert answer.reevaluated == case["reevaluated"]


def _by_name(case_name: str):
    case = next(c for c in TABLE["cases"] if c["name"] == case_name)
    return tool_contract(
        build(TABLE["recorded"]), CLUSTER, SERVER, proposed_by(TABLE["recorded"], case["mutations"])
    )


def test_removing_a_tool_db_admin_uses_names_db_admin():
    answer = _by_name("a tool every user requires is removed")

    action = next(f.action for f in answer.findings if f.agent == "db-admin")
    assert "db-admin" in action and "execute_sql" in action


def test_adding_a_tool_names_nobody():
    answer = _by_name("a tool is added")

    assert answer.findings == [] and answer.reevaluated == []


def test_a_changed_required_argument_names_the_users_of_that_tool():
    answer = _by_name("an argument becomes required")

    assert {f.agent for f in answer.findings} == {"sql-helper", "upgrading"}
    assert all("explain_query" in f.action and f.agent in f.action for f in answer.findings)


def test_a_server_with_no_backend_has_no_users():
    proposed = proposed_by(TABLE["recorded"], [{"op": "remove-tool", "tool": "execute_sql"}])

    answer = tool_contract(build(None, with_backend=False), CLUSTER, SERVER, proposed)

    assert (answer.backend, answer.users, answer.findings, answer.blocked) == (None, 0, [], False)


def test_a_user_with_two_releases_has_one_finding_per_tool():
    proposed = proposed_by(TABLE["recorded"], [{"op": "remove-tool", "tool": "execute_sql"}])

    answer = tool_contract(build(TABLE["recorded"]), CLUSTER, SERVER, proposed)

    assert [f.tool for f in answer.findings if f.agent == "upgrading"] == ["execute_sql"]
    assert answer.users == 4


@pytest.mark.parametrize("what", ["cluster", "server"])
def test_an_unknown_cluster_or_server_is_not_found(what):
    store = build(TABLE["recorded"])
    arguments = {"cluster": CLUSTER, "server": SERVER, what: "nothing"}

    with pytest.raises(Refusal) as refused:
        tool_contract(store, arguments["cluster"], arguments["server"], TABLE["recorded"])

    assert refused.value.code == "not-found"
