"""Release artifacts for one built image (spec 6.2, 6.3, 14.1).

The pipeline pushes the image, then calls this with the pushed digest. The output is what
Kubani consumes: the Agent, an AgentRelease with approval false, the compiled policy bundle
for security review, and the attestation predicate cosign attaches to the image.

`lockSha256` is the SHA-256 of the repository's `uv.lock`: the one workspace lock every
agent image in the repository installs from. It moves whenever any dependency in the
workspace moves; `buildRevision` and `manifestSha256` say whether the agent itself did.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml
from agent_platform.bundle import canonical_json, compile_bundle, digest_hex

from zetra_agents.layout import AgentDir

API_VERSION = "agentplatform.io/v1alpha1"
PREDICATE_TYPE = "https://agentplatform.io/attestations/release/v1"
DIGEST_REF = re.compile(r"^[a-z0-9.\-:/]+@sha256:([a-f0-9]{64})$")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def release_name(agent: str, image: str, date: dt.date) -> str:
    match = DIGEST_REF.match(image)
    if not match:
        raise ValueError(f"image must be pinned by digest: {image!r}")
    return f"{agent}-{date.isoformat()}-{match.group(1)[:8]}"


def build(
    agent: AgentDir,
    *,
    image: str,
    revision: str,
    mcp_resource_base: str,
    trust_domain: str,
    date: dt.date | None = None,
) -> dict[str, Any]:
    cr = agent.agent_cr()
    tenant = cr["metadata"]["namespace"]
    name = release_name(agent.name, image, date or dt.datetime.now(dt.UTC).date())
    manifest_sha = file_sha256(agent.manifest_path)
    lock_sha = file_sha256(agent.lock_path)
    release: dict[str, Any] = {
        "apiVersion": API_VERSION,
        "kind": "AgentRelease",
        "metadata": {"name": name, "namespace": tenant},
        "spec": {
            "agent": agent.name,
            "image": image,
            "manifestSha256": manifest_sha,
            "policySha256": "",
            "lockSha256": lock_sha,
            "buildRevision": revision[:40],
            # Approval is a reviewed change in Kubani; a release is never published approved.
            "approval": {"approved": False},
        },
    }
    bundle = compile_bundle(
        cr, release, mcp_resource_base=mcp_resource_base.rstrip("/"), trust_domain=trust_domain
    )
    policy_sha = digest_hex(bundle)
    release["spec"]["policySha256"] = policy_sha
    return {
        "name": name,
        "agent": cr,
        "release": release,
        "bundle": bundle,
        "predicate": {
            "release": name,
            "manifestSha256": manifest_sha,
            "policySha256": policy_sha,
            "lockSha256": lock_sha,
            "buildRevision": revision[:40],
        },
    }


def write(artifacts: dict[str, Any], out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "agent.yaml": yaml.safe_dump(artifacts["agent"], sort_keys=False),
        "agentrelease.yaml": yaml.safe_dump(artifacts["release"], sort_keys=False),
        "policy-bundle.json": canonical_json(artifacts["bundle"]).decode() + "\n",
        "predicate.json": json.dumps(artifacts["predicate"], indent=2, sort_keys=True) + "\n",
    }
    written = []
    for filename, text in files.items():
        (out / filename).write_text(text, encoding="utf-8")
        written.append(out / filename)
    sums = "".join(f"{file_sha256(p)}  {p.name}\n" for p in written)
    (out / "SHA256SUMS").write_text(sums, encoding="utf-8")
    return [*written, out / "SHA256SUMS"]


PLACEHOLDER_DIGEST = "0" * 64


def compile_problems(agent: AgentDir, mcp_resource_base: str) -> list[str]:
    """Compile the agent's release policy against a placeholder image, as a release would.

    Catches what the layout checks cannot: an Agent that is inside the CRD and that the
    contracts still cannot compile (a backend declared twice), before a tag is pushed.
    """
    namespace = (agent.agent_cr().get("metadata") or {}).get("namespace")
    if not isinstance(namespace, str) or not namespace:
        if namespace is None or namespace == "":
            reason = "metadata.namespace is missing; it must be the tenant namespace (tenant-...)"
        else:
            reason = "metadata.namespace must be a string naming the tenant namespace (tenant-...)"
        return [f"{agent.name}: deploy/agent.yaml {reason}"]
    try:
        artifacts = build(
            agent,
            image=f"registry.almckay.io/agents/{agent.name}@sha256:{PLACEHOLDER_DIGEST}",
            revision=PLACEHOLDER_DIGEST[:40],
            mcp_resource_base=mcp_resource_base,
            trust_domain="cluster.local",
            date=dt.date(2000, 1, 1),
        )
    except KeyError as exc:
        return [
            f"{agent.name}: release policy does not compile: deploy/agent.yaml is missing the field {exc.args[0]!r}"
        ]
    except (ValueError, TypeError) as exc:
        return [f"{agent.name}: release policy does not compile: {exc}"]
    spec = artifacts["release"]["spec"]
    problems = []
    if spec["approval"] != {"approved": False}:
        problems.append(f"{agent.name}: a published release must be unapproved")
    if not re.fullmatch(r"[0-9a-f]{64}", spec["policySha256"]):
        problems.append(f"{agent.name}: policySha256 is not a sha256 hex digest")
    for backend, entry in artifacts["bundle"].get("mcp", {}).items():
        if entry.get("effect") != "remote_read":
            problems.append(
                f"{agent.name}: MCP grant {backend} has effect {entry.get('effect')!r}; only remote_read is admitted"
            )
    return problems
