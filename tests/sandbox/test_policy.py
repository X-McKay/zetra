import asyncio
import json
import os
import secrets
import subprocess
import uuid
from pathlib import Path

import grpc
import pytest
from openshell import SandboxClient, TlsConfig

from tests.fixtures.sandbox import OpenShellSession
from zetra._runtime.artifacts import FileStore, Tape
from zetra._runtime.openshell import PYTHON, policy
from zetra._runtime.workspace import Workspace

pytestmark = pytest.mark.cluster


@pytest.fixture
def gateway():
    tls = os.environ.get("OPENSHELL_TLS_DIR")
    if not tls:
        pytest.skip("explicit kind OpenShell TLS directory required")
    root = Path(tls)
    client = SandboxClient(
        os.environ.get("OPENSHELL_ADDRESS", "127.0.0.1:18080"),
        tls=TlsConfig(
            ca_path=root / "ca.crt", cert_path=root / "tls.crt", key_path=root / "tls.key"
        ),
    )
    yield client
    client.close()


async def test_policy_enforced_inside_real_workload_and_identity_scoped(gateway, tmp_path):
    ident = "policy-" + uuid.uuid4().hex
    store = FileStore(tmp_path / "objects")
    root = tmp_path / "base"
    root.mkdir()
    position = Tape(store, manifest="demo-v1").empty(Workspace(root, store).capture())
    runtime = OpenShellSession(
        gateway, store, session=ident, manifest="demo-v1", key=secrets.token_bytes(32)
    )
    binding = None
    try:
        binding = await runtime.provision(
            {"session": ident, "message": "policy check", "position": position.model_dump()}
        )
        code = """import json,pathlib,socket,os
results={}
p=pathlib.Path('/sandbox/allowed');p.write_text('ok');results['workspace']=p.read_text()=='ok'
for name,path,write in [('outside_read','/private-canary/readable',False),('outside_write','/restricted-write/file',True),('host_socket','/var/run/docker.sock',False)]:
 try:
  p=pathlib.Path(path)
  if write:p.write_text('bad')
  else:p.read_bytes()
  results[name]=False
 except OSError:results[name]=True
for name,address in json.loads(os.environ['PROBE_ENDPOINTS']).items():
 try:
  s=socket.create_connection(tuple(address),timeout=2);s.close();results[name]=False
 except OSError:results[name]=True
print(json.dumps(results))"""
        endpoints = {
            "metadata": ["169.254.169.254", 80],
            "unapproved_egress": ["1.1.1.1", 443],
            "temporal": [os.environ["TEMPORAL_CLUSTER_IP"], 7233],
            "management": [os.environ["OPENSHELL_CLUSTER_IP"], 8080],
        }
        result = await asyncio.to_thread(
            gateway.exec,
            binding["name"],
            [PYTHON, "-c", code],
            workspace="default",
            env={"PROBE_ENDPOINTS": json.dumps(endpoints)},
            timeout_seconds=20,
            no_login_shell=True,
        )
        assert result.exit_code == 0, result.stderr
        observed = json.loads(result.stdout)
        assert all(observed.values()), observed
        with pytest.raises(ValueError):
            await runtime.checked_exec({**binding, "sandbox_id": "stale"}, "rpc", b"{}")
        # No Temporal or management certificate is injected into workload env.
        result = await asyncio.to_thread(
            gateway.exec,
            binding["name"],
            [
                PYTHON,
                "-c",
                'import os; assert not any(k.startswith("TEMPORAL") for k in os.environ); print("ok")',
            ],
            workspace="default",
            timeout_seconds=10,
            no_login_shell=True,
        )
        assert result.exit_code == 0
    finally:
        assert (await runtime.close({"binding": binding}))["cleanup"] == "DELETED"


def test_gateway_rejects_client_without_certificate():
    tls = os.environ.get("OPENSHELL_TLS_DIR")
    if not tls:
        pytest.skip("explicit kind OpenShell TLS directory required")
    with (
        SandboxClient(
            os.environ.get("OPENSHELL_ADDRESS", "127.0.0.1:18080"),
            tls=TlsConfig(ca_path=Path(tls) / "ca.crt"),
            timeout=5,
        ) as client,
        pytest.raises(grpc.RpcError),
    ):
        list(client.list(workspace="default"))


async def test_agent_specific_policy_can_allow_only_the_named_canary(gateway, tmp_path):
    ident = "allow-" + uuid.uuid4().hex
    store = FileStore(tmp_path / "objects")
    root = tmp_path / "base"
    root.mkdir()
    position = Tape(store, manifest="allow-v1").empty(Workspace(root, store).capture())
    approved = policy()
    approved.filesystem.read_only.append("/private-canary")
    runtime = OpenShellSession(
        gateway,
        store,
        session=ident,
        manifest="allow-v1",
        key=secrets.token_bytes(32),
        sandbox_policy=approved,
    )
    binding = None
    try:
        binding = await runtime.provision(
            {"session": ident, "message": "policy check", "position": position.model_dump()}
        )
        result = await asyncio.to_thread(
            gateway.exec,
            binding["name"],
            [
                PYTHON,
                "-c",
                'from pathlib import Path; print(Path("/private-canary/readable").read_text())',
            ],
            workspace="default",
            timeout_seconds=10,
            no_login_shell=True,
        )
        assert result.exit_code == 0 and result.stdout.strip() == "denied"
    finally:
        assert (await runtime.close({"binding": binding}))["cleanup"] == "DELETED"


async def test_prebuilt_code_keeps_two_live_session_workspaces_fresh(gateway, tmp_path):
    """Code caches may be shared through the image; writable session state may not."""
    sessions = []
    probe = """import asyncio,json,pathlib,sys
import zetra._runtime.runtime
import google.genai.types
workspace=pathlib.Path('/sandbox/workspace')
assert not list(workspace.iterdir()), 'new session inherited workspace files'
assert not list(pathlib.Path('/sandbox/objects').iterdir()), 'new session inherited artifacts'
for path in [workspace/'session-marker',pathlib.Path('/tmp/session-marker')]:
 assert not path.exists(), 'new session inherited a marker'
 path.write_text(sys.argv[1])
caches=[pathlib.Path(m.__cached__) for m in [asyncio,zetra._runtime.runtime,google.genai.types]]
assert all(p.is_file() for p in caches), 'image lacks prebuilt trusted bytecode'
for path in caches:
 try:
  with path.open('ab'): pass
 except PermissionError: pass
 else: raise AssertionError('agent can modify trusted bytecode')
print(json.dumps({'marker':(workspace/'session-marker').read_text(),'caches':len(caches)}))"""
    try:
        for index in range(2):
            session = "fresh-" + uuid.uuid4().hex
            root = tmp_path / str(index)
            root.mkdir()
            base = root / "base"
            base.mkdir()
            store = FileStore(root / "objects")
            position = Tape(store, manifest="demo-v1").empty(Workspace(base, store).capture())
            remote = OpenShellSession(
                gateway, store, session=session, manifest="demo-v1", key=secrets.token_bytes(32)
            )
            start = {"session": session, "message": "freshness", "position": position.model_dump()}
            sessions.append((remote, start))
            binding = await remote.provision(start)
            result = await asyncio.to_thread(
                gateway.exec,
                binding["name"],
                [PYTHON, "-c", probe, session],
                workspace="default",
                timeout_seconds=20,
                no_login_shell=True,
            )
            assert result.exit_code == 0, result.stderr
            assert json.loads(result.stdout) == {"marker": session, "caches": 3}

        assert sessions[0][0].binding["sandbox_id"] != sessions[1][0].binding["sandbox_id"]
        resources = json.loads(
            await asyncio.to_thread(
                subprocess.check_output,
                [
                    "kubectl",
                    "--kubeconfig",
                    os.environ["BRIDGE_KUBECONFIG"],
                    "-n",
                    "openshell",
                    "get",
                    "pods,pvc",
                    "-o",
                    "json",
                ],
                text=True,
                timeout=15,
            )
        )["items"]
        objects = {(r["kind"], r["metadata"]["name"]): r for r in resources}
        identities = []
        for remote, start in sessions:
            pod = objects["Pod", "default--" + remote.name]
            pvc = objects["PersistentVolumeClaim", "workspace-default--" + remote.name]
            assert pvc["status"]["phase"] == "Bound"
            assert any(
                v.get("persistentVolumeClaim", {}).get("claimName") == pvc["metadata"]["name"]
                for v in pod["spec"]["volumes"]
            )
            identities.append(
                (pod["metadata"]["uid"], pvc["metadata"]["uid"], pvc["spec"]["volumeName"])
            )
            # A retry for this same session attaches to its existing instance;
            # provisioning must neither recreate it nor erase its marker.
            binding = remote.binding.copy()
            assert await remote.provision(start) == binding
            result = await asyncio.to_thread(
                gateway.exec,
                binding["name"],
                [
                    PYTHON,
                    "-c",
                    "from pathlib import Path; print(Path('/sandbox/workspace/session-marker').read_text())",
                ],
                workspace="default",
                timeout_seconds=10,
                no_login_shell=True,
            )
            assert result.exit_code == 0 and result.stdout.strip() == start["session"]
        assert identities[0][0] != identities[1][0]
        assert identities[0][1] != identities[1][1]
        assert identities[0][2] != identities[1][2]
    finally:
        for remote, start in sessions:
            assert (await remote.close({"start": start, "binding": remote.binding}))[
                "cleanup"
            ] == "DELETED"
