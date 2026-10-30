from __future__ import annotations

import copy
import json
import random

from agent_platform.bundle import (
    canonical_json,
    compile_bundle,
    delegation_required,
    digest_hex,
    policy_digest,
)
from agent_platform.cli import main as agentctl
from tests.conftest import FIXTURES


def build(agent, release, cfg):
    return compile_bundle(
        agent, release, mcp_resource_base=cfg.mcp_resource_base, trust_domain=cfg.trust_domain
    )


def shuffled(value, rng):
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {k: shuffled(v, rng) for k, v in items}
    if isinstance(value, list):
        return [shuffled(v, rng) for v in value]
    return value


def test_bundle_matches_section_6_3_shape(agent, release, cfg):
    bundle = build(agent, release, cfg)
    assert bundle == {
        "version": 1,
        "tenant": "tenant-a",
        "agent": "investigator",
        "release": "investigator-2026-10-29-a93557ad",
        "image": release["spec"]["image"],
        "client_id": "agent-tenant-a-investigator",
        "spiffe_id": "spiffe://cluster.local/ns/tenant-a/sa/agent-investigator",
        "identity": {"mode": "on-behalf-of", "required": True, "fallback": "deny"},
        "routes": {
            "inference.agents.internal": ["/models/llm-main/v1/"],
            "mcp.agents.internal": ["/kubernetes/mcp"],
            "token.agents.internal": ["/v1/token"],
        },
        "models": {"llm-main": ["Qwen3.6-35B-A3B-NVFP4"]},
        "mcp": {
            "kubernetes": {
                "effect": "remote_read",
                "tools": ["events_list", "pods_get", "pods_list_in_namespace"],
                "delegation": "on-behalf-of",
                "resource": "https://mcp.example.com/kubernetes/mcp",
            }
        },
        "budgets": {"tokens_per_day": 5000000, "tokens_per_session": 200000},
        "runtime": {"ttl_seconds": 1800, "max_boundaries": 64},
    }
    assert delegation_required(bundle)


def test_canonical_json_has_sorted_keys_and_no_whitespace(agent, release, cfg):
    raw = canonical_json(build(agent, release, cfg))
    assert b" " not in raw.replace(b"Read-only", b"")
    assert json.loads(raw) == build(agent, release, cfg)
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()
    assert policy_digest(build(agent, release, cfg)) == "sha256:" + digest_hex(json.loads(raw))


def test_t20_digest_stable_under_key_reordering(agent, release, cfg):
    want = digest_hex(build(agent, release, cfg))
    rng = random.Random(7)
    for _ in range(20):
        assert digest_hex(build(shuffled(agent, rng), shuffled(release, rng), cfg)) == want


def test_t20_digest_stable_under_tool_order(agent, release, cfg):
    want = digest_hex(build(agent, release, cfg))
    for tools in (
        ["events_list", "pods_get", "pods_list_in_namespace"],
        ["pods_get", "events_list", "pods_list_in_namespace"],
    ):
        a = copy.deepcopy(agent)
        a["spec"]["capabilities"]["mcp"][0]["tools"] = tools
        assert digest_hex(build(a, release, cfg)) == want


def test_crd_defaults_applied_by_compiler(agent, release, cfg):
    # The API server fills CRD defaults; the YAML in git does not. Both digests agree.
    defaulted = copy.deepcopy(agent)
    defaulted["spec"]["enabled"] = True
    defaulted["spec"]["runtime"].update(runtimeClass="kata-qemu-runtime-rs", maxBoundaries=64)
    bare = copy.deepcopy(agent)
    del bare["spec"]["identity"]["onBehalfOf"]
    del bare["spec"]["capabilities"]["mcp"][0]["delegation"]
    del bare["spec"]["capabilities"]["models"][0]["budget"]["tokensPerSession"]
    del bare["spec"]["runtime"]
    assert digest_hex(build(defaulted, release, cfg)) == digest_hex(build(bare, release, cfg))


def test_enabled_flag_is_not_part_of_the_bundle(agent, release, cfg):
    disabled = copy.deepcopy(agent)
    disabled["spec"]["enabled"] = False
    assert digest_hex(build(disabled, release, cfg)) == digest_hex(build(agent, release, cfg))


def test_inherit_resolves_to_identity_mode(agent, release, cfg):
    agent["spec"]["identity"] = {"mode": "agent"}
    agent["spec"]["capabilities"]["mcp"].append(
        {"backend": "github", "effect": "remote_read", "tools": ["x"], "delegation": "on-behalf-of"}
    )
    bundle = build(agent, release, cfg)
    assert bundle["mcp"]["kubernetes"]["delegation"] == "agent"
    assert bundle["mcp"]["github"]["delegation"] == "on-behalf-of"
    assert bundle["identity"] == {"mode": "agent", "required": True, "fallback": "deny"}
    assert not delegation_required(bundle)
    assert bundle["routes"]["mcp.agents.internal"] == ["/github/mcp", "/kubernetes/mcp"]


def test_routes_derived_from_capabilities(agent, release, cfg):
    agent["spec"]["capabilities"] = {}
    bundle = build(agent, release, cfg)
    assert bundle["routes"] == {"token.agents.internal": ["/v1/token"]}
    assert bundle["budgets"] == {"tokens_per_day": 2000000, "tokens_per_session": 200000}


def test_t20_any_capability_change_changes_digest(agent, release, cfg):
    base = digest_hex(build(agent, release, cfg))
    widened = copy.deepcopy(agent)
    widened["spec"]["capabilities"]["mcp"][0]["tools"].append("secrets_get")
    narrowed = copy.deepcopy(agent)
    narrowed["spec"]["capabilities"]["mcp"][0]["tools"].pop()
    budget = copy.deepcopy(agent)
    budget["spec"]["capabilities"]["models"][0]["budget"]["tokensPerDay"] = 1
    for changed in (widened, narrowed, budget):
        assert digest_hex(build(changed, release, cfg)) != base


def test_agentctl_policy_digest_matches_compiler(agent, release, cfg, capsys):
    rc = agentctl(
        [
            "policy-digest",
            "--agent",
            str(FIXTURES / "agent.yaml"),
            "--release",
            str(FIXTURES / "release.yaml"),
            "--mcp-resource-base",
            "https://mcp.example.com",
        ]
    )
    assert rc == 0
    assert capsys.readouterr().out.strip() == digest_hex(build(agent, release, cfg))


def test_release_for_other_agent_is_refused(agent, release, cfg):
    release["spec"]["agent"] = "other"
    try:
        build(agent, release, cfg)
    except ValueError:
        return
    raise AssertionError("expected ValueError")
