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
