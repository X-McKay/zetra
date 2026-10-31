"""agentctl: build-time helpers that must agree byte for byte with the controller."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from agent_platform.bundle import compile_bundle, digest_hex, policy_digest
from agent_platform.constants import DEFAULT_TRUST_DOMAIN, PlatformConfig
from agent_platform.netpol import netpol_digest, render_tenant_policies
from agent_platform.podtemplate import render_pod_template
from agent_platform.signing import (
    GITHUB_ACTIONS_ISSUER,
    PREDICATE_EVAL,
    PREDICATE_VULN,
    SignerIdentity,
    eval_problems,
    subject_regexp_for,
    vuln_problems,
)

IMAGE_RE = re.compile(r"^[a-z0-9.:/-]+@sha256:[a-f0-9]{64}$")
# Placeholders for the deployment inputs: they shape env values, never the checks.
LOCAL_PLATFORM = dict(
    issuer_url="https://idp.example.invalid/realms/agents",
    gateway_cluster_ip="10.43.250.10",
    egress_guard_image="registry.example.invalid/platform/egress-guard@sha256:" + "0" * 64,
)


def _load(path: str, kind: str) -> dict[str, Any]:
    docs = [d for d in yaml.safe_load_all(Path(path).read_text()) if d]
    matches = [d for d in docs if d.get("kind") == kind]
    if len(matches) != 1:
        raise SystemExit(f"{path}: expected exactly one {kind}, found {len(matches)}")
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentctl")
    sub = parser.add_subparsers(dest="command", required=True)

    digest = sub.add_parser("policy-digest", help="print spec.policySha256 for a release")
    digest.add_argument("--agent", required=True, help="Agent YAML")
    digest.add_argument("--release", required=True, help="AgentRelease YAML")
    digest.add_argument(
        "--mcp-resource-base",
        default=os.environ.get("AGENT_PLATFORM_MCP_RESOURCE_BASE"),
        help="https://mcp.<domain> (env AGENT_PLATFORM_MCP_RESOURCE_BASE)",
    )
    digest.add_argument(
        "--trust-domain",
        default=os.environ.get("AGENT_PLATFORM_TRUST_DOMAIN", DEFAULT_TRUST_DOMAIN),
    )
    digest.add_argument("--print-bundle", action="store_true", help="also print the bundle")

    netpol = sub.add_parser("render-netpol", help="print the tenant NetworkPolicies and digest")
    netpol.add_argument("--namespace", required=True)

    validate = sub.add_parser(
        "validate", help="run the checks CI and admission run, without a cluster"
    )
    validate.add_argument("--agent", required=True, help="Agent YAML")
    validate.add_argument("--release", required=True, help="AgentRelease YAML")
    validate.add_argument(
        "--mcp-resource-base",
        default=os.environ.get("AGENT_PLATFORM_MCP_RESOURCE_BASE"),
        help="https://mcp.<domain> (env AGENT_PLATFORM_MCP_RESOURCE_BASE)",
    )
    validate.add_argument(
        "--trust-domain",
        default=os.environ.get("AGENT_PLATFORM_TRUST_DOMAIN", DEFAULT_TRUST_DOMAIN),
    )
    validate.add_argument(
        "--policies",
        help="directory of Kyverno baseline policies; runs `kyverno apply` on the sandbox pod",
    )
    validate.add_argument("--eval", help="eval/v1 predicate JSON to check")
    validate.add_argument("--vuln", help="vuln/v1 predicate JSON to check")
    validate.add_argument(
        "--vuln-exception", action="append", default=[], help="allowed high finding id"
    )
    validate.add_argument("--out", help="write the rendered pod and NetworkPolicies here")

    signer = sub.add_parser(
        "signer-annotations", help="print the tenant namespace's signer annotations"
    )
    signer.add_argument("--repo", required=True, help="owner/name of the agent repo")
    signer.add_argument("--workflow", default="release.yml")
    signer.add_argument(
        "--agent",
        action="append",
        default=[],
        help="pin to this agent's release tags (repeatable); needed when a repo serves "
        "more than one tenant",
    )
    signer.add_argument("--issuer", default=GITHUB_ACTIONS_ISSUER)

    args = parser.parse_args(argv)
    if args.command == "validate":
        if not args.mcp_resource_base:
            parser.error("--mcp-resource-base or AGENT_PLATFORM_MCP_RESOURCE_BASE is required")
        problems = validate_release(args)
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        if not problems:
            print("OK")
        return 1 if problems else 0
    if args.command == "signer-annotations":
        identity = SignerIdentity(
            args.issuer, subject_regexp_for(args.repo, args.workflow, tuple(args.agent))
        )
        print(yaml.safe_dump({"annotations": identity.annotations()}, sort_keys=False), end="")
        return 0
    if args.command == "policy-digest":
        if not args.mcp_resource_base:
            parser.error("--mcp-resource-base or AGENT_PLATFORM_MCP_RESOURCE_BASE is required")
        bundle = compile_bundle(
            _load(args.agent, "Agent"),
            _load(args.release, "AgentRelease"),
            mcp_resource_base=args.mcp_resource_base.rstrip("/"),
            trust_domain=args.trust_domain,
        )
        if args.print_bundle:
            print(yaml.safe_dump(bundle, sort_keys=True), file=sys.stderr)
        print(digest_hex(bundle))
        return 0

    print(f"# agentplatform.io/netpol-digest: {netpol_digest()}")
    print(yaml.safe_dump_all(render_tenant_policies(args.namespace), sort_keys=False), end="")
    return 0


def validate_release(args: argparse.Namespace) -> list[str]:
    problems: list[str] = []
    try:
        agent = _load(args.agent, "Agent")
        release = _load(args.release, "AgentRelease")
    except SystemExit as exc:
        return [str(exc)]
    spec = release.get("spec") or {}
    if spec.get("agent") != agent["metadata"].get("name"):
        problems.append("AgentRelease spec.agent does not name the Agent")
    if agent["metadata"].get("namespace") != release["metadata"].get("namespace"):
        problems.append("Agent and AgentRelease are in different namespaces")
    if not IMAGE_RE.match(str(spec.get("image", ""))):
        problems.append("spec.image must be pinned as <repo>@sha256:<64 hex>")
    for field in ("manifestSha256", "policySha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", str(spec.get(field, ""))):
            problems.append(f"spec.{field} must be 64 lowercase hex")

    try:
        bundle = compile_bundle(
            agent,
            release,
            mcp_resource_base=args.mcp_resource_base.rstrip("/"),
            trust_domain=args.trust_domain,
        )
    except (ValueError, KeyError) as exc:
        return [*problems, f"policy bundle does not compile: {exc}"]
    if digest_hex(bundle) != spec.get("policySha256"):
        problems.append(f"spec.policySha256 should be {digest_hex(bundle)}")

    namespace = agent["metadata"].get("namespace", "")
    try:
        cfg = PlatformConfig(
            mcp_resource_base=args.mcp_resource_base.rstrip("/"),
            trust_domain=args.trust_domain,
            **LOCAL_PLATFORM,
        )
        pod = render_pod_template(agent, release, bundle, policy_digest(bundle), cfg)
        netpols = render_tenant_policies(namespace)
    except (ValueError, KeyError) as exc:
        return [*problems, f"sandbox pod does not render: {exc}"]

    out = Path(args.out) if args.out else None
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "pod.yaml").write_text(yaml.safe_dump(pod, sort_keys=False))
        (out / "netpol.yaml").write_text(yaml.safe_dump_all(netpols, sort_keys=False))

    if args.eval:
        problems += [f"eval: {p}" for p in eval_problems(_json(args.eval, PREDICATE_EVAL))]
    if args.vuln:
        predicate = _json(args.vuln, PREDICATE_VULN)
        problems += [f"vuln: {p}" for p in vuln_problems(predicate, tuple(args.vuln_exception))]
    if args.policies:
        problems += _kyverno_apply(args.policies, pod, out)
    return problems


def _json(path: str, predicate_type: str) -> Any:
    """A bare predicate, or an in-toto statement wrapping one."""
    doc = json.loads(Path(path).read_text())
    if isinstance(doc, dict) and "predicate" in doc and "predicateType" in doc:
        if doc["predicateType"] != predicate_type:
            return None
        return doc["predicate"]
    return doc


def _kyverno_apply(policies: str, pod: dict[str, Any], out: Path | None) -> list[str]:
    """Shape rules only. Pass a directory without the verifyImages policies: an unsigned
    local image cannot pass them, and CI runs them against the real release."""
    kyverno = shutil.which("kyverno")
    if kyverno is None:
        return ["--policies given but the kyverno CLI is not on PATH"]
    with tempfile.TemporaryDirectory() as tmp:
        resource = (out or Path(tmp)) / "pod.yaml"
        resource.write_text(yaml.safe_dump(pod, sort_keys=False))
        proc = subprocess.run(
            [kyverno, "apply", policies, "--resource", str(resource)],
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode == 0:
        return []
    detail = (proc.stdout + proc.stderr).strip().splitlines()[-20:]
    return [f"kyverno apply exited {proc.returncode}", *(f"  {line}" for line in detail)]


if __name__ == "__main__":
    sys.exit(main())
