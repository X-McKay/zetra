"""Offline compatibility checks use candidate files, never live cluster state. The source
checkout is laid out as every agent repository is: agents/<name>/ members of one workspace."""

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from zetra_agents import cli

AGENT = "db-admin"
TAG = "db-admin/v0.1.2"
REPOSITORY = "X-McKay/zetra-agents"
ISSUER = "https://token.actions.githubusercontent.com"
SUBJECT = "https://github.com/X-McKay/zetra-agents/.github/workflows/release.yml@refs/tags/"
PATTERN = r"^https://github\.com/X-McKay/zetra-agents/\.github/workflows/release\.yml@refs/tags/(?:db-admin|other)/v[0-9][A-Za-z0-9.+-]*$"
BASE = "infrastructure/gitops/"
FILES = {
    "candidate": BASE + "apps/agents/tenant-test/db-admin/agent.yaml",
    "aps": BASE + "apps/agent-platform/workloads/agent-policy-service.yaml",
    "namespace": BASE + "infrastructure/namespace-policy/agent-platform-namespaces.yaml",
    "pins": BASE + "infrastructure/kyverno/tenant-signers.yaml",
}


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value))


@pytest.fixture
def inputs(tmp_path):
    source, kubani = tmp_path / "source", tmp_path / "kubani"
    cr = {
        "kind": "Agent",
        "metadata": {"name": AGENT, "namespace": "tenant-test"},
        "spec": {
            "capabilities": {
                "models": [
                    {
                        "backend": "llm-main",
                        "models": ["test"],
                        "budget": {
                            "tokensPerDay": 1000000,
                            "tokensPerSession": 200000,
                        },
                    }
                ],
                "mcp": [
                    {
                        "backend": "postgres-admin",
                        "effect": "remote_read",
                        "tools": ["execute_sql"],
                        "delegation": "inherit",
                    }
                ],
            }
        },
    }
    agent = source / "agents" / AGENT
    write(agent / "agent.yaml", {"name": AGENT, "model": {"backend": "llm-main", "name": "test"}})
    write(agent / "deploy/agent.yaml", cr)
    (agent / "pyproject.toml").touch()
    for name in ("pyproject.toml", "uv.lock"):
        (source / name).touch()
    write(kubani / FILES["candidate"], cr)
    write(
        kubani / FILES["aps"],
        {
            "kind": "Deployment",
            "metadata": {"name": "agent-policy-service"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "agent-policy-service",
                                "env": [
                                    {
                                        "name": "APS_BACKEND_CATALOGUE",
                                        "value": json.dumps({"llm-main": {"maxModelLen": 131072}}),
                                    }
                                ],
                            }
                        ]
                    }
                }
            },
        },
    )
    write(
        kubani / FILES["namespace"],
        {
            "kind": "Namespace",
            "metadata": {
                "name": "tenant-test",
                "annotations": {
                    "agentplatform.io/signer-issuer": ISSUER,
                    "agentplatform.io/signer-subject-regexp": PATTERN,
                },
            },
        },
    )
    write(
        kubani / FILES["pins"],
        {
            "kind": "ConfigMap",
            "metadata": {"name": "tenant-signers", "namespace": "kyverno"},
            "data": {"tenant-test": json.dumps({"issuer": ISSUER, "subjectRegExp": PATTERN})},
        },
    )
    for repo in (source, kubani):
        git(repo, "init", "-b", "main")
        git(repo, "add", ".")
        git(
            repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "fixture",
        )
    return source, kubani


def run(inputs, capsys, tag=TAG, repository=REPOSITORY):
    source, kubani = inputs
    argv = ["--repo", str(source), "preflight", AGENT, "--kubani", str(kubani), "--tag", tag]
    if repository:
        argv += ["--repository", repository]
    status = cli.main(argv)
    result = json.loads(capsys.readouterr().out)
    assert status == (0 if result["ok"] else 1)
    return result


def change(path, mutate):
    doc = yaml.safe_load(path.read_text())
    mutate(doc)
    write(path, doc)


def codes(result):
    return {e["code"] for e in result["errors"]}


def test_cli_preflight_success_receipt(inputs, capsys):
    before = [git(p, "status", "--porcelain") for p in inputs]
    result = run(inputs, capsys)
    assert result["ok"] and result["schemaVersion"] == 1
    assert result["scope"] == "candidate-desired-gitops"
    receipt = result["receipt"]
    assert receipt["subject"] == SUBJECT + TAG
    assert receipt["repository"] == REPOSITORY
    for label, path in zip(("source", "kubani"), inputs, strict=True):
        assert receipt[label]["head"] == git(path, "rev-parse", "HEAD")
        assert receipt[label]["dirty"] is False
        assert receipt[label]["main"]["ancestor"] is True
    for item in receipt["files"]:
        assert hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() == item["sha256"]
    assert before == [git(p, "status", "--porcelain") for p in inputs]
    assert all(not git(p, "tag") for p in inputs)


@pytest.mark.parametrize(
    "section,field,value",
    [
        ("mcp", "tools", ["write"]),
        ("mcp", "effect", "remote_write"),
        ("mcp", "delegation", "none"),
        ("models", "models", ["other"]),
        ("models", "backend", "other"),
        ("models", "budget", {"tokensPerDay": 3}),
    ],
)
def test_source_candidate_capability_drift(inputs, capsys, section, field, value):
    change(
        inputs[1] / FILES["candidate"],
        lambda d: d["spec"]["capabilities"][section][0].update({field: value}),
    )
    result = run(inputs, capsys)
    assert "capability_drift" in codes(result)
    assert any(field in e["path"] for e in result["errors"])


@pytest.mark.parametrize(
    "field,value,ok",
    [
        ("tokensPerDay", 131072, True),
        ("tokensPerSession", 131072, True),
        ("tokensPerDay", 131071, False),
        ("tokensPerSession", 131071, False),
        ("tokensPerSession", 100000, False),
        ("tokensPerDay", True, False),
        ("tokensPerDay", 0, False),
        ("tokensPerDay", -1, False),
        ("tokensPerDay", "200000", False),
    ],
)
def test_reservation_budget_boundaries(inputs, capsys, field, value, ok):
    change(
        inputs[1] / FILES["candidate"],
        lambda d: d["spec"]["capabilities"]["models"][0]["budget"].update({field: value}),
    )
    # Source must agree with candidate; commit it for faithful prospective tag binding.
    (inputs[0] / "agents/db-admin/deploy/agent.yaml").write_bytes(
        (inputs[1] / FILES["candidate"]).read_bytes()
    )
    git(inputs[0], "add", ".")
    git(
        inputs[0],
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "budget",
    )
    result = run(inputs, capsys)
    assert result["ok"] is ok
    if not ok:
        assert "budget" in codes(result)


@pytest.mark.parametrize("field", ["tokensPerDay", "tokensPerSession"])
@pytest.mark.parametrize("small_limit,ok", [(500, False), (800, True)])
@pytest.mark.parametrize("reverse", [False, True])
def test_effective_budget_funds_every_backend(inputs, capsys, field, small_limit, ok, reverse):
    # Each grant funds its own backend; the shared minimum must also fund the larger one.
    grants = [
        {
            "backend": "llm-main",
            "models": ["test"],
            "budget": {"tokensPerDay": 1000, "tokensPerSession": 1000, field: small_limit},
        },
        {
            "backend": "llm-large",
            "models": ["test-large"],
            "budget": {"tokensPerDay": 1000, "tokensPerSession": 1000},
        },
    ]
    if reverse:
        grants.reverse()
    change(
        inputs[1] / FILES["candidate"],
        lambda d: d["spec"]["capabilities"].update(models=grants),
    )
    change(
        inputs[1] / FILES["aps"],
        lambda d: d["spec"]["template"]["spec"]["containers"][0]["env"][0].update(
            value=json.dumps({"llm-main": {"maxModelLen": 300}, "llm-large": {"maxModelLen": 800}})
        ),
    )
    (inputs[0] / "agents/db-admin/deploy/agent.yaml").write_bytes(
        (inputs[1] / FILES["candidate"]).read_bytes()
    )
    git(inputs[0], "add", ".")
    git(
        inputs[0],
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-m",
        "models",
    )
    result = run(inputs, capsys)
    assert result["ok"] is ok
    if not ok:
        assert "budget" in codes(result)


@pytest.mark.parametrize("tag", ["other/v1", "db-admin/v", "refs/heads/db-admin/v1", "../v1"])
def test_invalid_prospective_tag(inputs, capsys, tag):
    assert "tag" in codes(run(inputs, capsys, tag))


@pytest.mark.parametrize("pattern", ["", "db-admin", "^[invalid$", "^reject$"])
def test_signer_pattern_failures(inputs, capsys, pattern):
    change(
        inputs[1] / FILES["namespace"],
        lambda d: d["metadata"]["annotations"].update(
            {"agentplatform.io/signer-subject-regexp": pattern}
        ),
    )
    change(
        inputs[1] / FILES["pins"],
        lambda d: d["data"].update(
            {"tenant-test": json.dumps({"issuer": ISSUER, "subjectRegExp": pattern})}
        ),
    )
    assert "signer" in codes(run(inputs, capsys))


@pytest.mark.parametrize(
    "field,value", [("issuer", "https://other.example"), ("subjectRegExp", "^other$")]
)
def test_disagreeing_signer_pins(inputs, capsys, field, value):
    change(
        inputs[1] / FILES["pins"],
        lambda d: d["data"].update(
            {"tenant-test": json.dumps({"issuer": ISSUER, "subjectRegExp": PATTERN, field: value})}
        ),
    )
    assert "signer" in codes(run(inputs, capsys))


@pytest.mark.parametrize("key", list(FILES))
@pytest.mark.parametrize(
    "text",
    [
        "key: 1\nkey: 2\n",
        "nested: {x: 1, x: 2}\n",
        "base: &b {x: 1}\nobj: {<<: *b}\n",
        "[broken",
        "null",
        "[]",
    ],
)
def test_strict_input_rejections(inputs, capsys, key, text):
    (inputs[1] / FILES[key]).write_text(text)
    result = run(inputs, capsys)
    assert not result["ok"]
    assert all(len(e["message"]) < 300 for e in result["errors"])
    assert "[broken" not in json.dumps(result)


@pytest.mark.parametrize("key", ["aps", "pins"])
def test_duplicate_embedded_json(inputs, capsys, key):
    def mutate(d):
        if key == "aps":
            d["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = (
                '{"llm-main": {}, "llm-main": {}}'
            )
        else:
            d["data"]["tenant-test"] = '{"issuer": "a", "issuer": "b"}'

    change(inputs[1] / FILES[key], mutate)
    assert "input" in codes(run(inputs, capsys))


@pytest.mark.parametrize("key", list(FILES))
def test_missing_and_ambiguous_resource(inputs, capsys, key):
    path = inputs[1] / FILES[key]
    path.write_text(path.read_text() + "\n---\n" + path.read_text())
    assert "input" in codes(run(inputs, capsys))
    path.unlink()
    assert "input" in codes(run(inputs, capsys))


def test_checkout_binding(inputs, capsys):
    source, kubani = inputs
    (kubani / "candidate-note").write_text("candidate differs intentionally")
    result = run(inputs, capsys)
    assert result["ok"] and result["receipt"]["kubani"]["dirty"]
    git(kubani, "checkout", "-b", "candidate")
    git(kubani, "branch", "-D", "main")
    assert run(inputs, capsys)["receipt"]["kubani"]["main"] is None
    (source / "dirty").touch()
    assert "dirty_source" in codes(run(inputs, capsys))
    (source / "dirty").unlink()
    git(source, "tag", TAG)
    assert "existing_tag" in codes(run(inputs, capsys))


def test_selected_layout_failure(inputs, capsys):
    (inputs[0] / "agents/db-admin/pyproject.toml").unlink()
    assert "layout" in codes(run(inputs, capsys))


def test_repository_defaults_to_the_github_origin(inputs, capsys):
    source = inputs[0]
    assert "repository" in codes(run(inputs, capsys, repository=None))
    git(source, "remote", "add", "origin", "git@github.com:X-McKay/my-agent.git")
    result = run(inputs, capsys, repository=None)
    assert result["receipt"]["repository"] == "X-McKay/my-agent"
    assert result["receipt"]["subject"].startswith("https://github.com/X-McKay/my-agent/")
    # The tenant pin names zetra-agents, so a release from another repository is refused.
    assert "signer" in codes(result)
    git(source, "remote", "set-url", "origin", "https://github.com/X-McKay/zetra-agents")
    assert run(inputs, capsys, repository=None)["ok"]
    assert "repository" in codes(run(inputs, capsys, repository="not a repo"))


def test_source_strict_duplicate(inputs, capsys):
    path = inputs[0] / "agents/db-admin/agent.yaml"
    path.write_text("name: db-admin\nname: other\n")
    assert "input" in codes(run(inputs, capsys))


def test_resource_selection_ignores_other_names(inputs, capsys):
    for key, document in [
        ("namespace", {"kind": "Namespace", "metadata": {"name": "agent-platform"}}),
        ("aps", {"kind": "Deployment", "metadata": {"name": "unrelated"}}),
        ("pins", {"kind": "ConfigMap", "metadata": {"name": "other", "namespace": "kyverno"}}),
    ]:
        path = inputs[1] / FILES[key]
        path.write_text(yaml.safe_dump(document) + "---\n" + path.read_text())
    assert run(inputs, capsys)["ok"]


@pytest.mark.parametrize("value", [None, [], "bad"])
def test_malformed_source_model_is_structured(inputs, capsys, value):
    change(inputs[0] / "agents/db-admin/agent.yaml", lambda d: d.update({"model": value}))
    result = run(inputs, capsys)
    assert not result["ok"]
    assert "input" in codes(result) or "layout" in codes(result)


@pytest.mark.parametrize(
    "catalogue",
    [
        {},
        {"llm-main": {"maxModelLen": True}},
        {"llm-main": {"maxModelLen": 0}},
        {"llm-main": {"maxModelLen": -1}},
        {"llm-main": {"maxModelLen": "131072"}},
    ],
)
def test_unknown_or_invalid_backend(inputs, capsys, catalogue):
    change(
        inputs[1] / FILES["aps"],
        lambda d: d["spec"]["template"]["spec"]["containers"][0]["env"][0].update(
            {"value": json.dumps(catalogue)}
        ),
    )
    assert "input" in codes(run(inputs, capsys))


@pytest.mark.parametrize("entry", ["containers", "env"])
def test_ambiguous_aps_entries(inputs, capsys, entry):
    def mutate(d):
        containers = d["spec"]["template"]["spec"]["containers"]
        items = containers if entry == "containers" else containers[0]["env"]
        items.append(copy.deepcopy(items[0]))

    change(inputs[1] / FILES["aps"], mutate)
    assert "input" in codes(run(inputs, capsys))


def test_local_main_divergence_informational(inputs, capsys):
    kubani = inputs[1]
    git(kubani, "checkout", "-b", "candidate")
    git(
        kubani,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "candidate",
    )
    git(kubani, "checkout", "main")
    git(
        kubani,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-m",
        "other main",
    )
    git(kubani, "checkout", "candidate")
    result = run(inputs, capsys)
    assert result["ok"] and result["receipt"]["kubani"]["main"]["ancestor"] is False


def test_read_only_git_and_timeout(inputs, capsys, monkeypatch):
    from zetra_agents import preflight

    original = subprocess.run
    calls = []

    def observe(args, **kwargs):
        assert args[:2] == ["git", "--no-optional-locks"]
        assert args[4] in {"rev-parse", "status", "merge-base", "tag"}
        if args[4] == "tag":
            assert args[5] == "--list"
        assert kwargs["timeout"] == 10 and not kwargs.get("shell")
        calls.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(preflight.subprocess, "run", observe)
    assert run(inputs, capsys)["ok"] and calls

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 10)

    monkeypatch.setattr(preflight.subprocess, "run", timeout)
    assert "input" in codes(run(inputs, capsys))


def test_unrelated_conformance_not_checked(inputs, capsys):
    (inputs[0] / "agents/conformance").mkdir()
    assert run(inputs, capsys)["ok"]


def test_non_git_input_is_structured(inputs, capsys, tmp_path):
    status = cli.main(
        ["--repo", str(tmp_path), "preflight", AGENT, "--kubani", str(inputs[1]), "--tag", TAG]
    )
    assert status == 1 and not json.loads(capsys.readouterr().out)["ok"]


def test_nonstandard_json_constant_rejected(inputs, capsys):
    change(
        inputs[1] / FILES["pins"],
        lambda d: d["data"].update(
            {
                "tenant-test": json.dumps({"issuer": ISSUER, "subjectRegExp": PATTERN})[:-1]
                + ', "extra": NaN}'
            }
        ),
    )
    assert "input" in codes(run(inputs, capsys))


def test_source_python_syntax_error_is_sanitized(inputs, capsys):
    path = inputs[0] / "agents/db-admin/src/broken.py"
    path.parent.mkdir()
    path.write_text("def private_source_marker(\n")
    result = run(inputs, capsys)
    assert {
        "code": "input",
        "path": "source.layout",
        "message": "cannot read or validate input",
    } in result["errors"]
    assert "private_source_marker" not in json.dumps(result)
    assert capsys.readouterr().err == ""


def test_source_multidocument_layout_error_is_sanitized(inputs, capsys):
    path = inputs[0] / "agents/db-admin/deploy/agent.yaml"
    path.write_text(
        path.read_text() + "---\nkind: ConfigMap\ndata: {private_yaml_marker: hidden}\n"
    )
    result = run(inputs, capsys)
    assert {
        "code": "input",
        "path": "source.layout",
        "message": "cannot read or validate input",
    } in result["errors"]
    assert "private_yaml_marker" not in json.dumps(result)
    assert capsys.readouterr().err == ""
