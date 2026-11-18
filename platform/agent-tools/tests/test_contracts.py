"""`check --contracts DIR`: an agent's tool grants against the tool contracts of the tool
servers (dependencies.md section 11)."""

import json

import pytest
import yaml

from zetra_agents import cli, layout, render

REV = "0123456789abcdef0123456789abcdef01234567"
AGENT = "demo-agent"


def lock_text(revision):
    entries = []
    for name, sub in render.PINNED.items():
        git = f"{render.ZETRA_GIT}?subdirectory={sub.replace('/', '%2F')}&rev={revision}#{revision}"
        entries.append(
            f'[[package]]\nname = "{name}"\nversion = "0.1.0"\nsource = {{ git = "{git}" }}\n'
        )
    return "version = 1\n\n" + "\n".join(entries)


@pytest.fixture
def repo(tmp_path, capsys):
    target = tmp_path / "repo"
    assert (
        cli.main(
            [
                "init",
                str(target),
                "--agent",
                AGENT,
                "--owner",
                "dev@example.com",
                "--zetra-rev",
                REV,
                "--no-lock",
                "--no-git",
            ]
        )
        == 0
    )
    capsys.readouterr()
    (target / "uv.lock").write_text(lock_text(REV))
    return target


def grant(repo, *grants):
    """Give the agent these tool grants in its deploy/agent.yaml."""
    path = layout.find(repo, AGENT).agent_cr_path
    cr = yaml.safe_load(path.read_text())
    cr["spec"]["capabilities"]["mcp"] = list(grants)
    path.write_text(yaml.safe_dump(cr, sort_keys=False))


def contract(directory, server, backend, tools):
    """A contract file of section 2.1 for a server, with the tools as (name, effect) pairs."""
    directory.mkdir(exist_ok=True)
    document = {
        "schemaVersion": 1,
        "server": server,
        "backend": backend,
        "image": "docker.io/example/server@sha256:" + "ab" * 32,
        "configDigest": "sha256:" + "cd" * 32,
        "tools": [
            {
                "name": name,
                "description": name,
                "effect": effect,
                "inputSchema": {"type": "object", "properties": {}},
                "outputSchema": None,
            }
            for name, effect in tools
        ],
    }
    (directory / f"{server}.json").write_text(json.dumps(document, sort_keys=True))


def check(repo, *args):
    return cli.main(["--repo", str(repo), "check", *args])


def test_a_granted_tool_the_contract_does_not_list_fails_naming_agent_backend_and_tool(
    repo, tmp_path, capsys
):
    grant(
        repo,
        {"backend": "postgres-admin", "effect": "remote_read", "tools": ["execute_sql", "drop"]},
    )
    contract(
        tmp_path / "c", "postgres-admin-mcp", "postgres-admin", [("execute_sql", "remote_read")]
    )
    assert check(repo, "--contracts", str(tmp_path / "c")) == 1
    err = capsys.readouterr().err
    assert AGENT in err and "postgres-admin" in err and "drop" in err


def test_a_granted_tool_whose_effect_differs_fails(repo, tmp_path, capsys):
    grant(repo, {"backend": "postgres-admin", "effect": "remote_read", "tools": ["execute_sql"]})
    contract(
        tmp_path / "c", "postgres-admin-mcp", "postgres-admin", [("execute_sql", "remote_write")]
    )
    assert check(repo, "--contracts", str(tmp_path / "c")) == 1
    err = capsys.readouterr().err
    assert AGENT in err and "postgres-admin" in err and "execute_sql" in err
    assert "remote_write" in err


def test_a_grant_whose_backend_no_file_names_passes_and_is_reported_as_not_checked(
    repo, tmp_path, capsys
):
    grant(repo, {"backend": "kubernetes", "effect": "remote_read", "tools": ["pods_list"]})
    contract(
        tmp_path / "c", "postgres-admin-mcp", "postgres-admin", [("execute_sql", "remote_read")]
    )
    assert check(repo, "--contracts", str(tmp_path / "c")) == 0
    out = capsys.readouterr().out
    assert "kubernetes" in out and "not checked" in out


def test_an_agent_whose_grants_all_match_passes(repo, tmp_path, capsys):
    grant(repo, {"backend": "postgres-admin", "effect": "remote_read", "tools": ["execute_sql"]})
    contract(
        tmp_path / "c",
        "postgres-admin-mcp",
        "postgres-admin",
        [("execute_sql", "remote_read"), ("list_tables", "remote_read")],
    )
    assert check(repo, "--contracts", str(tmp_path / "c")) == 0
    assert capsys.readouterr().out.strip() == f"ok: {AGENT}"


def test_without_contracts_the_same_repository_passes_as_before(repo, capsys):
    grant(repo, {"backend": "postgres-admin", "effect": "remote_read", "tools": ["drop"]})
    assert check(repo) == 0
    assert capsys.readouterr().out.strip() == f"ok: {AGENT}"


def test_a_file_that_is_not_a_contract_fails_naming_the_file(repo, tmp_path, capsys):
    directory = tmp_path / "c"
    directory.mkdir()
    (directory / "broken.json").write_text("{not json")
    assert check(repo, "--contracts", str(directory)) == 1
    assert "broken.json" in capsys.readouterr().err
