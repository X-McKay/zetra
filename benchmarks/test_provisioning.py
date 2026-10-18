"""Opt-in, interleaved fresh-sandbox comparison; no latency thresholds or inference.

BRIDGE_PROVISIONING=1 BRIDGE_PROVISION_BASELINE_IMAGE=zetra-validation:before-bytecode \
  uv run python scripts/kind_test.py benchmarks/test_provisioning.py -s -x
"""

import asyncio
import hashlib
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from openshell import SandboxClient, TlsConfig

from tests.fixtures.sandbox import OpenShellSession
from zetra._runtime.artifacts import FileStore, Tape
from zetra._runtime.openshell import PYTHON
from zetra._runtime.vllm import MANIFEST
from zetra._runtime.workspace import Workspace

PROBE = """import time,resource
begin=time.perf_counter();cpu=resource.getrusage(resource.RUSAGE_SELF).ru_utime
import zetra._runtime.runtime
elapsed=time.perf_counter()-begin;cpu=resource.getrusage(resource.RUSAGE_SELF).ru_utime-cpu
import json,pathlib,asyncio
import google.genai.types
assert not list(pathlib.Path('/sandbox/workspace').iterdir())
assert not list(pathlib.Path('/sandbox/objects').iterdir())
print(json.dumps({'import_seconds':elapsed,'import_user_cpu_seconds':cpu,
 'prebuilt_bytecode':[pathlib.Path(m.__cached__).is_file() for m in
 [asyncio,zetra._runtime.runtime,google.genai.types]]}))"""


class ObservedClient:
    """Times SDK operations, including the pinned native create RPC."""

    def __init__(self, client):
        self.client = client
        self.calls = []
        self._stub = SimpleNamespace(
            CreateSandbox=self.observe("create", client._stub.CreateSandbox)
        )

    def __getattr__(self, name):
        method = getattr(self.client, name)
        if name not in ("get", "create", "wait_ready", "exec"):
            return method

        return self.observe(name, method)

    def observe(self, name, method):
        def observed(*args, **kwargs):
            begin = time.perf_counter()
            try:
                return method(*args, **kwargs)
            finally:
                self.calls.append({"method": name, "seconds": time.perf_counter() - begin})

        return observed


@pytest.mark.cluster
async def test_fresh_provisioning_comparison(tmp_path):
    if os.environ.get("BRIDGE_PROVISIONING") != "1":
        pytest.skip("opt-in comparison requires two preloaded images")
    images = {
        "baseline": os.environ["BRIDGE_PROVISION_BASELINE_IMAGE"],
        "candidate": os.environ.get("BRIDGE_PROVISION_CANDIDATE_IMAGE", "zetra-validation:local"),
    }
    assert images["baseline"] != images["candidate"]
    tls = Path(os.environ["OPENSHELL_TLS_DIR"])
    client = SandboxClient(
        os.environ["OPENSHELL_ADDRESS"],
        tls=TlsConfig(ca_path=tls / "ca.crt", cert_path=tls / "tls.crt", key_path=tls / "tls.key"),
    )
    output = Path(os.environ.get("BRIDGE_PROVISION_OUTPUT", ".state/provisioning.json"))
    result = {"images": images, "samples": []}
    order = ["baseline", "candidate", "candidate", "baseline", "baseline", "candidate"]
    trials = [("scripted", variant) for variant in order]
    trials += [("vllm", variant) for variant in ["candidate", "baseline", "baseline", "candidate"]]
    identities = set()
    try:
        for index, (provider, variant) in enumerate(trials):
            root = tmp_path / str(index)
            root.mkdir()
            base = root / "base"
            base.mkdir()
            store = FileStore(root / "objects")
            manifest = MANIFEST if provider == "vllm" else "demo-v1"
            session = "provision-" + uuid.uuid4().hex
            observed = ObservedClient(client)
            remote = OpenShellSession(
                observed,
                store,
                session=session,
                manifest=manifest,
                key=secrets.token_bytes(32),
                provider=provider,
                image=images[variant],
            )
            start = {
                "session": session,
                "message": "Inspect",
                "position": Tape(store, manifest=manifest)
                .empty(Workspace(base, store).capture())
                .model_dump(),
            }
            try:
                begin = time.perf_counter()
                binding = await remote.provision(start)
                elapsed = time.perf_counter() - begin
                calls = list(observed.calls)
                assert sum(call["method"] == "create" for call in calls) == 1
                assert binding["sandbox_id"] not in identities
                identities.add(binding["sandbox_id"])
                probe = await asyncio.to_thread(
                    client.exec,
                    binding["name"],
                    [PYTHON, "-c", PROBE],
                    workspace="default",
                    timeout_seconds=30,
                    no_login_shell=True,
                )
                assert probe.exit_code == 0, probe.stderr
                imported = json.loads(probe.stdout)
                if variant == "candidate":
                    assert all(imported["prebuilt_bytecode"])
                else:
                    assert not all(imported["prebuilt_bytecode"])
                result["samples"].append(
                    {
                        "provider": provider,
                        "variant": variant,
                        "sandbox_id": binding["sandbox_id"],
                        "provision_seconds": elapsed,
                        "calls": calls,
                        "fresh_import": imported,
                        "policy_sha256": hashlib.sha256(
                            remote.policy.SerializeToString(deterministic=True)
                        ).hexdigest(),
                    }
                )
                print(f"fresh {provider} {variant}: {elapsed:.3f}s", flush=True)
            finally:
                assert (await remote.close({"start": start, "binding": remote.binding}))[
                    "cleanup"
                ] == "DELETED"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2) + "\n")
        for provider in ("scripted", "vllm"):
            assert (
                len({r["policy_sha256"] for r in result["samples"] if r["provider"] == provider})
                == 1
            )
    finally:
        client.close()
