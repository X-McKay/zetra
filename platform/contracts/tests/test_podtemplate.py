"""T22 / T3 logic: the rendered pod satisfies every field the 6.5 admission rules pin.

The Kyverno policy is loaded verbatim from the spec (fixtures/kyverno-6.5.yaml) and its
`pattern` and `deny` parts are evaluated by a small local evaluator. One difference from
Kyverno is deliberate and recorded: for a pattern list of several maps this evaluator
compares element by element (the spec's intent for `volumes`), whereas Kyverno applies
only the first map element to every resource element; see the controller report.
"""

from __future__ import annotations

import copy
import fnmatch
from pathlib import Path

import pytest
import yaml

from agent_platform.bundle import compile_bundle, policy_digest
from agent_platform.podtemplate import instantiate, render_pod_template

FIXTURES = Path(__file__).parent / "fixtures"
RULES = {
    r["name"]: r
    for r in yaml.safe_load((FIXTURES / "kyverno-6.5.yaml").read_text())["spec"]["rules"]
}
SESSION = "6f1d2c3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"


def _substitute(pattern: str, pod: dict) -> str:
    var = '{{request.object.metadata.labels."agentplatform.io/agent"}}'
    return pattern.replace(var, pod["metadata"]["labels"].get("agentplatform.io/agent", ""))


def matches(pattern, value, pod) -> bool:
    if isinstance(pattern, dict):
        return isinstance(value, dict) and all(
            k in value and matches(v, value[k], pod) for k, v in pattern.items()
        )
    if isinstance(pattern, list):
        if not isinstance(value, list):
            return False
        if len(pattern) == 1 and isinstance(pattern[0], dict):
            return all(matches(pattern[0], v, pod) for v in value)
        return len(value) >= len(pattern) and all(
            matches(p, v, pod) for p, v in zip(pattern, value)
        )
    if isinstance(pattern, bool):
        return isinstance(value, bool) and pattern == value
    if isinstance(pattern, int):
        return isinstance(value, int) and not isinstance(value, bool) and pattern == value
    pattern = _substitute(pattern, pod)
    text = str(value).lower() if isinstance(value, bool) else str(value)
    if pattern.startswith("!"):
        return not fnmatch.fnmatchcase(text, pattern[1:])
    return fnmatch.fnmatchcase(text, pattern)


def no_extras_violations(pod) -> list[str]:
    spec = pod["spec"]
    out = []
    if len(spec.get("containers", [])) != 1:
        out.append("containers")
    if len(spec.get("initContainers", [])) != 1:
        out.append("initContainers")
    vols = spec.get("volumes", [])
    if len(vols) != 5:
        out.append("volumes")
    for kind in ("hostPath", "secret", "persistentVolumeClaim"):
        if any(kind in v for v in vols):
            out.append(kind)
    return out


def admitted(pod) -> bool:
    return (
        matches(RULES["tenant-pods-are-sandboxes"]["validate"]["pattern"], pod, pod)
        and matches(RULES["sandbox-shape"]["validate"]["pattern"], pod, pod)
        and not no_extras_violations(pod)
    )


@pytest.fixture
def pod(agent, release, cfg):
    bundle = compile_bundle(
        agent, release, mcp_resource_base=cfg.mcp_resource_base, trust_domain=cfg.trust_domain
    )
    template = render_pod_template(agent, release, bundle, policy_digest(bundle), cfg)
    return instantiate(template, SESSION, 1)


def test_t22_rendered_pod_satisfies_every_admission_pattern(pod):
    assert matches(RULES["tenant-pods-are-sandboxes"]["validate"]["pattern"], pod, pod)
    assert matches(RULES["sandbox-shape"]["validate"]["pattern"], pod, pod)
    assert no_extras_violations(pod) == []


def test_t22_pinned_fields_explicitly(pod, cfg):
    spec = pod["spec"]
    assert spec["runtimeClassName"] == "kata-qemu-runtime-rs"
    assert spec["automountServiceAccountToken"] is False
    assert spec["serviceAccountName"] == "agent-investigator"
    assert spec["dnsPolicy"] == "None" and spec["dnsConfig"] == {"nameservers": ["127.0.0.1"]}
    assert [c["name"] for c in spec["initContainers"]] == ["egress-guard"]
    assert [c["name"] for c in spec["containers"]] == ["agent"]
    assert spec["containers"][0]["command"] == ["/app/.venv/bin/python", "-m", "agent_runtime.wait"]
    assert spec["initContainers"][0]["image"] == cfg.egress_guard_image
    assert [v["name"] for v in spec["volumes"]] == [
        "agent-identity",
        "gateway-ca",
        "podinfo",
        "workspace",
        "tmp",
    ]
    assert "subPath" not in yaml.safe_dump(pod)


def test_t3_only_identity_material_is_the_projected_token(pod, cfg):
    spec = pod["spec"]
    sources = spec["volumes"][0]["projected"]["sources"]
    assert sources == [
        {
            "serviceAccountToken": {
                "audience": cfg.issuer_url,
                "expirationSeconds": 600,
                "path": "token",
            }
        }
    ]
    assert spec["volumes"][0]["projected"]["defaultMode"] == 0o440
    env = {e["name"]: e["value"] for e in spec["containers"][0]["env"]}
    assert all("valueFrom" not in e for e in spec["containers"][0]["env"])
    assert env["AGENT_GATEWAY_URL"] == "https://10.43.250.10:443"
    assert env["AGENT_CLIENT_ID"] == "agent-tenant-a-investigator"
    assert env["AGENT_MODEL_ROUTE"] == "/models/llm-main"
    assert env["AGENT_TOKENS_PER_SESSION"] == "200000"
    assert env["AGENT_SESSION_ID"] == SESSION


def test_per_session_fields_only(agent, release, cfg, pod):
    bundle = compile_bundle(
        agent, release, mcp_resource_base=cfg.mcp_resource_base, trust_domain=cfg.trust_domain
    )
    template = render_pod_template(agent, release, bundle, policy_digest(bundle), cfg)
    assert pod["metadata"]["name"].startswith("s-") and pod["metadata"]["name"].endswith("-g1")
    assert pod["metadata"]["labels"]["agentplatform.io/session"] == SESSION
    assert pod["metadata"]["labels"]["agentplatform.io/generation"] == "1"
    assert pod["metadata"]["annotations"] == {
        "agentplatform.io/policy-digest": policy_digest(bundle)
    }
    # The template itself has empty session fields and so is not admissible as-is.
    assert not admitted(template)
    assert admitted(pod)


def test_resources_follow_agent_runtime(agent, release, cfg):
    agent["spec"]["runtime"] = {"cpu": "250m", "memory": "512Mi"}
    bundle = compile_bundle(
        agent, release, mcp_resource_base=cfg.mcp_resource_base, trust_domain=cfg.trust_domain
    )
    pod = render_pod_template(agent, release, bundle, policy_digest(bundle), cfg)
    res = pod["spec"]["containers"][0]["resources"]
    assert res == {
        "requests": {"cpu": "250m", "memory": "512Mi"},
        "limits": {"cpu": "250m", "memory": "512Mi"},
    }


def _drop(path):
    def f(p):
        *head, last = path
        node = p
        for k in head:
            node = node[k]
        del node[last]

    return f


def _set(path, value):
    def f(p):
        *head, last = path
        node = p
        for k in head:
            node = node[k]
        node[last] = value

    return f


T22_CASES = {
    "extra container": lambda p: p["spec"]["containers"].append(
        dict(copy.deepcopy(p["spec"]["containers"][0]), name="sidecar")
    ),
    "changed init image": _set(
        ["spec", "initContainers", 0, "image"], "registry.example.com/platform/egress-guard:latest"
    ),
    "hostPath volume": lambda p: p["spec"]["volumes"].append(
        {"name": "host", "hostPath": {"path": "/"}}
    ),
    "secret volume replaces tmp": _set(
        ["spec", "volumes", 4], {"name": "tmp", "secret": {"secretName": "x"}}
    ),
    "different serviceAccountName": _set(["spec", "serviceAccountName"], "default"),
    "no sandbox label": _drop(["metadata", "labels", "agentplatform.io/sandbox"]),
    "automount true": _set(["spec", "automountServiceAccountToken"], True),
    "automount omitted": _drop(["spec", "automountServiceAccountToken"]),
    "runtimeClassName omitted": _drop(["spec", "runtimeClassName"]),
    "hostNetwork true": _set(["spec", "hostNetwork"], True),
    "privileged": _set(["spec", "containers", 0, "securityContext", "privileged"], True),
    "dns default": _set(["spec", "dnsPolicy"], "ClusterFirst"),
    "other command": _set(["spec", "containers", 0, "command"], ["/bin/sh"]),
    "no policy digest": _drop(["metadata", "annotations", "agentplatform.io/policy-digest"]),
}


@pytest.mark.parametrize("case", sorted(T22_CASES))
def test_t22_mutations_are_rejected(pod, case):
    bad = copy.deepcopy(pod)
    T22_CASES[case](bad)
    assert not admitted(bad), case
