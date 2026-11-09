"""Offline source/candidate compatibility; no live-state or remote-freshness claims."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from zetra_agents import layout

ISSUER = "https://token.actions.githubusercontent.com"
GITOPS = Path("infrastructure/gitops")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GITHUB_REMOTE = re.compile(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def subject(repository: str, tag: str) -> str:
    """The Fulcio identity a release of `tag` from `repository` would be certified as."""
    return f"https://github.com/{repository}/.github/workflows/release.yml@refs/tags/{tag}"


def origin_repository(repo: Path) -> str | None:
    try:
        url = _git(repo, "remote", "get-url", "origin")
    except (subprocess.SubprocessError, OSError):
        return None
    match = GITHUB_REMOTE.search(url)
    return match.group(1) if match else None


class InputError(ValueError):
    """A bounded explanation safe to include in a receipt."""


class StrictLoader(yaml.SafeLoader):
    pass


def _mapping(loader: StrictLoader, node: yaml.MappingNode) -> dict:
    result = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise InputError("YAML merge keys are unsupported")
        key = loader.construct_object(key_node, deep=True)
        try:
            if key in result:
                raise InputError("duplicate YAML mapping key")
            result[key] = loader.construct_object(value_node, deep=True)
        except TypeError:
            raise InputError("invalid YAML mapping key") from None
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _pairs(items: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise InputError("duplicate JSON mapping key")
        result[key] = value
    return result


def _json(value: Any) -> dict:
    if not isinstance(value, str):
        raise InputError("expected literal JSON string")

    def invalid_constant(value: str) -> None:
        raise InputError("nonstandard JSON constant")

    result = json.loads(value, object_pairs_hook=_pairs, parse_constant=invalid_constant)
    return _dict(result)


def _dict(value: Any) -> dict:
    if not isinstance(value, dict):
        raise InputError("expected mapping")
    return value


def _list(value: Any) -> list:
    if not isinstance(value, list):
        raise InputError("expected list")
    return value


def _one(items: list, **fields: str) -> dict:
    def field(item: Any, key: str) -> Any:
        for part in key.split("."):
            item = _dict(item).get(part)
            if item is None:
                break
        return item

    matches = [item for item in items if all(field(item, k) == v for k, v in fields.items())]
    if len(matches) != 1:
        raise InputError("expected exactly one matching resource or entry")
    return matches[0]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()


def _checkout(repo: Path) -> dict:
    if Path(_git(repo, "rev-parse", "--show-toplevel")).resolve() != repo:
        raise InputError("checkout path must be its git repository root")
    head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    result = {
        "path": str(repo),
        "head": head,
        "dirty": bool(_git(repo, "status", "--porcelain", "--untracked-files=all")),
        "main": None,
    }
    for ref in ("refs/remotes/origin/main", "refs/heads/main"):
        try:
            tip = _git(repo, "rev-parse", "--verify", ref + "^{commit}")
        except subprocess.CalledProcessError:
            continue
        try:
            _git(repo, "merge-base", "--is-ancestor", tip, head)
            ancestor = True
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 1:
                raise
            ancestor = False
        result["main"] = {"ref": ref, "head": tip, "ancestor": ancestor}
        break
    return result


def _differences(left: Any, right: Any, path: str) -> list[str]:
    if type(left) is not type(right):
        return [path]
    if isinstance(left, dict):
        return [
            p
            for k in sorted(left.keys() | right.keys(), key=str)
            for p in (
                [f"{path}.{k}"]
                if k not in left or k not in right
                else _differences(left[k], right[k], f"{path}.{k}")
            )
        ]
    if isinstance(left, list):
        if len(left) != len(right):
            return [path]
        return [
            p
            for i, (a, b) in enumerate(zip(left, right, strict=True))
            for p in _differences(a, b, f"{path}[{i}]")
        ]
    return [] if left == right else [path]


def check(
    repo: Path, kubani: Path, agent: str, tag: str, repository: str | None = None
) -> dict[str, Any]:
    """Return a JSON-safe compatibility result for two explicit local checkouts.

    `repository` is the owner/name whose release workflow will sign; by default the
    source checkout's origin remote. The subject is what the tenant pin must accept.
    """
    repo, kubani = repo.resolve(), kubani.resolve()
    errors: list[dict[str, str]] = []
    receipt: dict[str, Any] = {"agent": agent, "tag": tag, "subject": None, "files": []}

    def error(code: str, path: str, message: str) -> None:
        errors.append({"code": code, "path": path[:240], "message": message[:240]})

    repository = repository or origin_repository(repo)
    if not repository or not REPOSITORY.match(repository):
        error("repository", "repository", "pass --repository owner/name; no GitHub origin found")
        return _result(receipt, errors)
    receipt["repository"] = repository
    receipt["subject"] = subject(repository, tag)

    def attempt(path: str, operation):
        try:
            return operation()
        except InputError as exc:
            error("input", path, str(exc))
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RecursionError,
            SyntaxError,
            yaml.YAMLError,
            subprocess.SubprocessError,
        ):
            error("input", path, "cannot read or validate input")
        return None

    def load(path: Path) -> list[dict]:
        data = path.read_bytes()
        receipt["files"].append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest()})
        try:
            docs = list(yaml.load_all(data, Loader=StrictLoader))
        except yaml.YAMLError:
            raise InputError("invalid YAML document") from None
        return [_dict(doc) for doc in docs if doc is not None]

    for name, path in (("source", repo), ("kubani", kubani)):
        info = attempt(name, lambda path=path: _checkout(path))
        receipt[name] = info
    if receipt["source"] and receipt["source"]["dirty"]:
        error("dirty_source", "source", "source checkout must be clean before prospective tagging")
    if not layout.NAME.fullmatch(agent):
        error("tag", "agent", "invalid agent name")
        return _result(receipt, errors)
    if not re.fullmatch(re.escape(agent) + r"/v[0-9][A-Za-z0-9.+-]*", tag):
        error("tag", "tag", "expected selected-agent/v<version> prospective tag")
    elif receipt["source"] and attempt("tag", lambda: _git(repo, "tag", "--list", tag)):
        error("existing_tag", "tag", "prospective tag already exists in source checkout")

    source_dir = repo / "agents" / agent
    manifest = attempt("source.agent.yaml", lambda: _one(load(source_dir / "agent.yaml")))
    source = attempt(
        "source.deploy/agent.yaml",
        lambda: _one(load(source_dir / "deploy/agent.yaml"), kind="Agent"),
    )
    if source is None or manifest is None:
        return _result(receipt, errors)
    problems = attempt("source.layout", lambda: layout.check(layout.find(repo, agent)))
    for problem in problems or []:
        # Existing layout diagnostics can contain source text; avoid exposing it.
        error("layout", "source", "selected agent failed existing layout check")
    namespace = attempt("source.metadata", lambda: _dict(source["metadata"])["namespace"])
    if not isinstance(namespace, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,61}[a-z0-9]|[a-z0-9]", namespace
    ):
        error("input", "source.metadata.namespace", "expected DNS namespace")
        return _result(receipt, errors)
    root = kubani / GITOPS
    candidate = attempt(
        "candidate.agent",
        lambda: _one(load(root / "apps/agents" / namespace / agent / "agent.yaml"), kind="Agent"),
    )
    if candidate is not None:

        def capabilities():
            if (
                _dict(candidate["metadata"]).get("name") != agent
                or candidate["metadata"].get("namespace") != namespace
            ):
                raise InputError("candidate Agent identity must match source")
            left = _dict(_dict(source["spec"])["capabilities"])
            right = _dict(_dict(candidate["spec"])["capabilities"])
            for path in _differences(left, right, "spec.capabilities"):
                error("capability_drift", path, "source and candidate capabilities differ")
            return right

        caps = attempt("candidate.capabilities", capabilities)
        if caps is not None:

            def budgets():
                deployment = _one(
                    load(root / "apps/agent-platform/workloads/agent-policy-service.yaml"),
                    kind="Deployment",
                    **{"metadata.name": "agent-policy-service"},
                )
                if deployment["metadata"]["name"] != "agent-policy-service":
                    raise InputError("expected agent-policy-service Deployment")
                container = _one(
                    _list(deployment["spec"]["template"]["spec"]["containers"]),
                    name="agent-policy-service",
                )
                env = _one(_list(container["env"]), name="APS_BACKEND_CATALOGUE")
                catalogue = _json(env["value"])
                grants = _list(caps["models"])
                reservations = []
                for grant in grants:
                    reservation = _dict(catalogue[_dict(grant)["backend"]])["maxModelLen"]
                    if type(reservation) is not int or reservation <= 0:
                        raise InputError("backend reservation must be a positive integer")
                    reservations.append(reservation)
                # The contract shares the smallest declared budget across all model entries.
                for index, grant in enumerate(grants):
                    budget = _dict(grant["budget"])
                    for field in ("tokensPerSession", "tokensPerDay"):
                        value = budget.get(field)
                        if type(value) is not int or value < max(reservations):
                            error(
                                "budget",
                                f"spec.capabilities.models[{index}].budget.{field}",
                                "budget must be an integer at least every backend reservation",
                            )

            attempt("candidate.aps", budgets)

    def signers():
        ns = _one(
            load(root / "infrastructure/namespace-policy/agent-platform-namespaces.yaml"),
            kind="Namespace",
            **{"metadata.name": namespace},
        )
        if ns["metadata"]["name"] != namespace:
            raise InputError("expected selected tenant Namespace")
        annotations = _dict(ns["metadata"]["annotations"])
        cm = _one(
            load(root / "infrastructure/kyverno/tenant-signers.yaml"),
            kind="ConfigMap",
            **{"metadata.name": "tenant-signers", "metadata.namespace": "kyverno"},
        )
        if (
            cm["metadata"].get("name") != "tenant-signers"
            or cm["metadata"].get("namespace") != "kyverno"
        ):
            raise InputError("expected kyverno tenant-signers ConfigMap")
        pin = _json(_dict(cm["data"])[namespace])
        pattern, issuer = pin.get("subjectRegExp"), pin.get("issuer")
        if (
            issuer != ISSUER
            or issuer != annotations.get("agentplatform.io/signer-issuer")
            or pattern != annotations.get("agentplatform.io/signer-subject-regexp")
        ):
            error(
                "signer",
                "candidate.signers",
                "tenant signer declarations must agree with GitHub issuer",
            )
            return
        if not isinstance(pattern, str) or not pattern.startswith("^") or not pattern.endswith("$"):
            error(
                "signer",
                "candidate.signers.subjectRegExp",
                "expected nonempty anchored signer pattern",
            )
            return
        try:
            accepted = re.fullmatch(pattern, receipt["subject"])
        except re.error:
            accepted = None
        if not accepted:
            error(
                "signer",
                "candidate.signers.subjectRegExp",
                "signer pattern must accept prospective workflow identity",
            )

    attempt("candidate.signers", signers)
    return _result(receipt, errors)


def _result(receipt: dict, errors: list) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "scope": "candidate-desired-gitops",
        "ok": not errors,
        "receipt": receipt,
        "errors": errors,
    }
