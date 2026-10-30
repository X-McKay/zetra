"""agentctl: build-time helpers that must agree byte for byte with the controller."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from agent_platform.bundle import compile_bundle, digest_hex
from agent_platform.constants import DEFAULT_TRUST_DOMAIN
from agent_platform.netpol import netpol_digest, render_tenant_policies


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

    args = parser.parse_args(argv)
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


if __name__ == "__main__":
    sys.exit(main())
