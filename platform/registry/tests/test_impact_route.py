import json

from agent_registry.rules.impact import tool_contract
from tests.test_rules_impact import SERVER, TABLE, build, proposed_by
from tests.world import CLUSTER, World

PATH = "/v1/checks/tool-contract"
REMOVE = [{"op": "remove-tool", "tool": "execute_sql"}]


def world(recorded=TABLE["recorded"]) -> World:
    found = World()
    found.store = build(recorded)
    found.app.state.store = found.store
    return found


def body(mutations=REMOVE, server=SERVER, cluster=CLUSTER, contract=None) -> dict:
    return {
        "cluster": cluster,
        "server": server,
        "contract": contract or proposed_by(TABLE["recorded"], mutations),
    }


def test_a_reader_gets_the_answer_for_a_blocking_case():
    found = world()
    before = found.store.generation()

    answer = found.post(PATH, body(), token="reader")

    assert answer.status_code == 200, answer.text
    out = answer.json()
    assert (out["server"], out["backend"], out["blocked"], out["users"]) == (
        SERVER,
        "postgres-admin",
        True,
        4,
    )
    first = out["findings"][0]
    assert first["deployment"] == "agent:kubani:tenant-test:db-admin"
    assert (first["agent"], first["tenant"], first["authority"]) == (
        "db-admin",
        "tenant-test",
        "registry",
    )
    assert (first["tool"], first["finding"], first["blocks"]) == (
        "execute_sql",
        "tool-removed",
        True,
    )
    assert (
        first["action"]
        == "keep execute_sql on postgres-admin, or release db-admin without it first"
    )
    assert out["reevaluated"] == []
    assert found.store.generation() == before


def test_an_operator_may_call_it_and_nobody_may_not():
    found = world()

    assert found.post(PATH, body(), token="operator").status_code == 200
    assert found.post(PATH, body(), token=None).status_code == 401
    assert found.post(PATH, body(), token="nobody").status_code == 403


def test_an_unknown_server_or_cluster_is_not_found():
    found = world()

    for answer in (
        found.post(PATH, body(server="nothing"), token="reader"),
        found.post(PATH, body(cluster="nothing"), token="reader"),
    ):
        assert answer.status_code == 404
        assert answer.json()["error"]["code"] == "not-found"


def test_a_contract_for_another_server_is_invalid_arguments():
    found = world()
    other = {**proposed_by(TABLE["recorded"], []), "server": "another-mcp"}
    no_tools = {k: v for k, v in proposed_by(TABLE["recorded"], []).items() if k != "tools"}

    for contract in (other, no_tools):
        answer = found.post(PATH, body(contract=contract), token="reader")
        assert answer.status_code == 400
        assert answer.json()["error"]["code"] == "invalid-arguments"


def test_the_route_answers_what_the_rule_does():
    found = world()
    proposed = proposed_by(TABLE["recorded"], REMOVE)

    answer = found.post(PATH, body(), token="reader").json()

    expected = tool_contract(found.store, CLUSTER, SERVER, proposed)
    assert answer == json.loads(expected.model_dump_json(by_alias=True))
