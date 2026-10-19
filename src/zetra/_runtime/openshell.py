"""Single-session OpenShell adapter. Official exec RPC carries bounded control messages.

Immutable artifacts are copied inline or separately and hash-verified in the
external store before an Activity can report success. Neither copy selects an accepted position.
"""

import asyncio
import base64
import hashlib
import json
import re
import time
from collections.abc import Collection
from contextlib import closing
from uuid import NAMESPACE_URL, uuid5

import grpc
from openshell import ExecChunk, ExecResult
from openshell._proto import openshell_pb2, sandbox_pb2
from openshell.sandbox import _sandbox_ref
from temporalio import activity

from .artifacts import Position, Ref
from .chunks import CHUNK_BYTES
from .inline import verify_inline
from .records import canonical_bytes
from .transport import Authority, ProtocolError

PYTHON = "/app/.venv/bin/python"
# Match the Workflow's provisioning schedule-to-close window; far below the
# gateway's 24-hour successful replay retention. This is not a server fence.
CREATE_WINDOW_SECONDS = 600


def normalize_providers(providers):
    """Canonical deployment-owned provider references; never credential values."""
    if (
        isinstance(providers, (str, bytes))
        or not isinstance(providers, Collection)
        or len(providers) > 8
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", name)
            for name in providers
        )
        or len(set(providers)) != len(providers)
    ):
        raise ValueError("providers must contain up to eight unique provider names")
    return tuple(sorted(providers))


def native_create(client, request, deadline):
    """Pinned OpenShell 0.1.4 shim: curated create() omits request_id.

    Reuse its authenticated, error-mapped stub and result conversion, avoiding a
    second connection/configuration or a copied SDK client. Remove this shim when
    the upstream public helper accepts request_id.
    """
    remaining = deadline - time.time()
    if remaining <= 0:
        raise ProtocolError("provisioning admission window expired")
    response = client._stub.CreateSandbox(request, timeout=min(client._timeout, remaining))
    ref = _sandbox_ref(response.sandbox, response.service_urls)
    if not ref.id:
        raise ProtocolError("CreateSandbox returned empty sandbox identity")
    return ref


def policy():
    return sandbox_pb2.SandboxPolicy(
        version=1,
        filesystem=sandbox_pb2.FilesystemPolicy(
            include_workdir=False,
            read_only=["/usr", "/bin", "/lib", "/lib64", "/etc", "/proc", "/app", "/dev/urandom"],
            read_write=["/sandbox", "/tmp", "/dev/null"],
        ),
        landlock=sandbox_pb2.LandlockPolicy(compatibility="hard_requirement"),
        process=sandbox_pb2.ProcessPolicy(run_as_user="1500", run_as_group="1500"),
    )


def vllm_policy():
    approved = policy()
    approved.network_policies["vllm"].CopyFrom(
        sandbox_pb2.NetworkPolicyRule(
            name="vllm-chat-only",
            endpoints=[
                sandbox_pb2.NetworkEndpoint(
                    host="llm.almckay.io",
                    port=443,
                    protocol="rest",
                    enforcement=sandbox_pb2.NETWORK_ENFORCEMENT_MODE_ENFORCE,
                    rules=[
                        sandbox_pb2.L7Rule(
                            allow=sandbox_pb2.L7Allow(method="POST", path="/v1/chat/completions")
                        )
                    ],
                )
            ],
            binaries=[sandbox_pb2.NetworkBinary(path="/usr/local/bin/python3.12")],
        )
    )
    return approved


class OpenShellSession:
    def __init__(
        self,
        client,
        store,
        *,
        session,
        manifest,
        key,
        image="zetra-validation:local",
        sandbox_policy=None,
        provider="scripted",
        agent_spec=None,
        runtime_module="zetra._runtime.runtime",
        providers=(),
    ):
        if provider not in ("scripted", "vllm"):
            raise ValueError("unsupported provider")
        if provider == "vllm" and agent_spec is None:
            from .vllm import MANIFEST

            if manifest != MANIFEST:
                raise ValueError("live profile manifest mismatch")
        self.agent_spec = agent_spec
        self.providers = normalize_providers(providers)
        self.runtime_module = runtime_module
        self.provider = provider
        self.client = client
        self.store = store
        self.session = session
        self.manifest = manifest
        self.key = key
        self.image = image
        self.policy = sandbox_policy or (vllm_policy() if provider == "vllm" else policy())
        self.release_labels = (
            {
                "zetra-authority": hashlib.sha256(key).hexdigest()[:63],
                "zetra-release": hashlib.sha256(manifest.encode()).hexdigest()[:63],
                "zetra-image": hashlib.sha256(image.encode()).hexdigest()[:63],
                "zetra-policy": hashlib.sha256(
                    self.policy.SerializeToString(deterministic=True)
                ).hexdigest()[:63],
            }
            if agent_spec is not None
            else {}
        )
        self.configure_generation(1)
        self.uploaded = set()
        self.binding = None
        self.creations = set()
        self.closing = False
        self.admission_deadline = time.time() + CREATE_WINDOW_SECONDS

    def verify_release(self, ref):
        if any(ref.labels.get(k) != v for k, v in self.release_labels.items()):
            raise ProtocolError("sandbox authority/release/image/policy mismatch")

    def configure_generation(self, generation):
        if type(generation) is not int or not 1 <= generation <= 64:
            raise ProtocolError("invalid generation")
        self.generation = generation
        self.authority = Authority(self.key, self.session, generation=generation)
        self.name = "b-" + hashlib.sha256(f"{self.session}:{generation}".encode()).hexdigest()[:16]

    def select_binding(self, binding):
        if binding["session"] != self.session:
            raise ProtocolError("wrong session")
        self.configure_generation(binding["generation"])
        if binding["name"] != self.name:
            raise ProtocolError("wrong binding name")
        self.binding = binding

    async def replace(self, command):
        closed = await self.close({"binding": command["binding"]})
        if closed["cleanup"] != "DELETED":
            raise ProtocolError("old writer not confirmed stopped")
        return await self.provision(
            {
                **command["start"],
                "position": command["position"],
                "generation": command["binding"]["generation"] + 1,
            }
        )

    async def create_settled(self, **kwargs):
        # Cancelling to_thread does not stop its RPC. Keep it tracked until the
        # bounded SDK call settles, including before terminal reconciliation.
        if self.closing:
            raise ProtocolError("session is closing")
        deadline = self.admission_deadline
        if activity.in_activity():
            # scheduled_time is the original Activity schedule, not this retry's
            # start. Reconstructed worker caches must not renew admission.
            deadline = activity.info().scheduled_time.timestamp() + CREATE_WINDOW_SECONDS
        workspace = kwargs.pop("workspace")
        request = openshell_pb2.CreateSandboxRequest(
            workspace_scope={"workspace": workspace},
            request_id=str(
                uuid5(NAMESPACE_URL, f"zetra/create/v1/{self.session}/{self.generation}")
            ),
            **kwargs,
        )
        task = asyncio.create_task(asyncio.to_thread(native_create, self.client, request, deadline))
        self.creations.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
            raise
        finally:
            if task.done():
                self.creations.discard(task)

    async def provision(self, start):
        if self.closing:
            raise ProtocolError("session is closing")
        self.configure_generation(start.get("generation", 1))
        if start["session"] != self.session:
            raise ValueError("wrong provision session")
        try:
            existing = await asyncio.to_thread(self.client.get, self.name, workspace="default")
        except grpc.RpcError as exc:
            if exc.code() != grpc.StatusCode.NOT_FOUND:
                raise
        else:
            self.verify_release(existing)
            if (
                existing.labels.get("bridge-session")
                != hashlib.sha256(self.session.encode()).hexdigest()[:32]
            ):
                raise ProtocolError("existing sandbox belongs to another session")
        # A name lookup alone cannot acknowledge an earlier admitted create.
        # Always replay the same logical request, including after worker loss.
        ref = await self.create_settled(
            workspace="default",
            name=self.name,
            labels={
                **self.release_labels,
                "bridge-session": hashlib.sha256(self.session.encode()).hexdigest()[:32],
                "bridge-generation": str(self.generation),
            },
            spec=openshell_pb2.SandboxSpec(
                providers=self.providers,
                template=openshell_pb2.SandboxTemplate(image=self.image),
                policy=self.policy,
                environment={
                    **({"ZETRA_AGENT": json.dumps(self.agent_spec)} if self.agent_spec else {}),
                    "BRIDGE_SESSION": self.session,
                    "BRIDGE_PROVIDER": self.provider,
                    "BRIDGE_GENERATION": str(self.generation),
                    "BRIDGE_MANIFEST": self.manifest,
                    "BRIDGE_KEY": self.key.hex(),
                    "BRIDGE_MESSAGE": start["message"],
                },
                command=[PYTHON, "-m", self.runtime_module],
            ),
        )
        if (
            ref.labels.get("bridge-session")
            != hashlib.sha256(self.session.encode()).hexdigest()[:32]
        ):
            raise ProtocolError("existing sandbox belongs to another session")
        self.verify_release(ref)
        if self.release_labels and ref.labels.get("bridge-generation") != str(self.generation):
            raise ProtocolError("sandbox generation mismatch")
        self.binding = {
            "session": self.session,
            "generation": self.generation,
            "name": self.name,
            "sandbox_id": ref.id,
            "position": start["position"],
        }
        return self.binding

    async def wait_ready(self, binding):
        """Observe an acknowledged instance; never create or replace a sandbox."""
        self.select_binding(binding)
        current = await asyncio.to_thread(self.client.get, self.name, workspace="default")
        self.verify_release(current)
        if (
            current.id != binding["sandbox_id"]
            or current.labels.get("bridge-session")
            != hashlib.sha256(self.session.encode()).hexdigest()[:32]
            or current.labels.get("bridge-generation") != str(self.generation)
        ):
            raise ProtocolError("sandbox readiness identity mismatch")
        ready = await asyncio.to_thread(
            self.client.wait_ready, self.name, workspace="default", timeout_seconds=180
        )
        self.verify_release(ready)
        if ready.id != binding["sandbox_id"] or ready.phase != 2:
            raise ProtocolError("sandbox readiness identity or phase mismatch")
        # Readiness means the supervisor is ready; wait for this service's socket.
        result = await asyncio.to_thread(
            self.client.exec,
            self.name,
            [
                PYTHON,
                "-c",
                (
                    'import pathlib,time; p=pathlib.Path("/sandbox/bridge.sock"); '
                    '\nfor _ in range(100):\n if p.exists(): break\n time.sleep(.1)\nelse: raise RuntimeError("runtime socket not ready")'
                ),
            ],
            workspace="default",
            timeout_seconds=15,
            no_login_shell=True,
        )
        if result.exit_code:
            raise RuntimeError(result.stderr[:300])
        return self.binding

    async def checked_exec(self, binding, mode, data):
        if binding["session"] != self.session or binding["name"] != self.name:
            raise ProtocolError("wrong binding")
        current = await asyncio.to_thread(self.client.get, self.name, workspace="default")
        self.verify_release(current)
        if current.id != binding["sandbox_id"]:
            raise ProtocolError("stale sandbox identity")
        command = [PYTHON, "-m", "zetra._runtime.remote_io", mode]
        maximum = Ref.model_validate_json(data).byte_length if mode == "get" else 262144

        name = binding["name"]

        def bounded():
            stdout = []
            size = 0
            errors = 0
            completed = None
            with closing(
                self.client.exec_stream(
                    name,
                    command,
                    workspace="default",
                    stdin=data,
                    timeout_seconds=40,
                    no_login_shell=True,
                )
            ) as stream:
                for item in stream:
                    if isinstance(item, ExecChunk):
                        if item.stream == "stdout":
                            size += len(item.data)
                            if size > maximum:
                                raise ProtocolError("exec output too large")
                            stdout.append(item.data)
                        else:
                            errors += len(item.data)
                            if errors > 65536:
                                raise ProtocolError("exec diagnostics too large")
                    elif isinstance(item, ExecResult):
                        completed = item
            if completed is None or completed.exit_code:
                raise RuntimeError(
                    "sandbox RPC failed: "
                    + (completed.stderr[:300] if completed else "missing completion")
                )
            return b"".join(stdout)

        return await asyncio.to_thread(bounded)

    def references(self, position):
        position = Position.model_validate(position)
        refs = {}
        if position.workspace:
            refs[position.workspace.sha256] = position.workspace
        current = position.head
        while current:
            if current.sha256 in refs:
                raise ValueError("tape cycle")
            refs[current.sha256] = current
            node = json.loads(self.store.get(current))
            for key in ("operation", "outcome"):
                if "ref" in node[key]:
                    ref = Ref.model_validate(node[key]["ref"])
                    refs[ref.sha256] = ref
            current = Ref.model_validate(node["previous"]) if node["previous"] else None
        return list(refs.values())

    async def call(self, action, command):
        binding = command["binding"]
        self.select_binding(binding)
        if action == "advance":
            for ref in await asyncio.to_thread(self.references, command["position"]):
                if (binding["sandbox_id"], ref.sha256) not in self.uploaded:
                    data = await asyncio.to_thread(self.store.get, ref)
                    for offset in range(0, max(1, len(data)), CHUNK_BYTES):
                        response = await self.checked_exec(
                            binding,
                            "put-chunk",
                            canonical_bytes(
                                {
                                    "ref": ref.model_dump(),
                                    "offset": offset,
                                    "data": base64.b64encode(
                                        data[offset : offset + CHUNK_BYTES]
                                    ).decode(),
                                }
                            ),
                        )
                    uploaded = json.loads(response)
                    if not uploaded.get("complete") or Ref.model_validate(uploaded["ref"]) != ref:
                        raise ProtocolError("artifact upload mismatch")
                    self.uploaded.add((binding["sandbox_id"], ref.sha256))
        envelope = self.authority.sign(action, command, ttl=45)
        data = await self.checked_exec(binding, "rpc", canonical_bytes(envelope))
        if len(data) > 262144:
            raise ProtocolError("control response too large")
        response = json.loads(data)
        if "ok" not in response:
            raise ProtocolError(response.get("message", "remote failure"))
        result = response["ok"]
        if action == "execute":
            bundled = verify_inline(result["objects"], result.get("inline_objects", []))
            for raw in result["objects"]:
                ref = Ref.model_validate(raw)
                data = bundled.get(ref.sha256)
                if data is None:
                    data = await self.checked_exec(
                        binding, "get", canonical_bytes(ref.model_dump())
                    )
                if await asyncio.to_thread(self.store.put, data) != ref:
                    raise ProtocolError("artifact download mismatch")
                self.uploaded.add((binding["sandbox_id"], ref.sha256))
            return result["receipt"]
        return result

    async def close(self, command):
        if "start" in command:
            if command["start"]["session"] != self.session:
                raise ProtocolError("wrong cleanup session")
            return await self.close_all()
        binding = command.get("binding")
        if binding:
            self.select_binding(binding)
        try:
            current = await asyncio.to_thread(self.client.get, self.name, workspace="default")
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                return {"cleanup": "DELETED"}
            raise
        if (
            current.labels.get("bridge-session")
            != hashlib.sha256(self.session.encode()).hexdigest()[:32]
        ):
            return {"cleanup": "REMEDIATION_REQUIRED"}
        self.verify_release(current)
        if binding and current.id != binding["sandbox_id"]:
            return {"cleanup": "REMEDIATION_REQUIRED"}
        # OpenShell 0.1.4 stop orders supervisor shutdown before workload removal.
        # Direct deletion tears them down concurrently and can consume the whole
        # Kubernetes grace period after a used workload loses its control channel.
        # Stop is only an optimization: it is never proof of session deletion.
        name = self.name
        try:
            await asyncio.to_thread(self.client.stop, name, workspace="default")
        except grpc.RpcError as exc:
            if exc.code() not in (
                grpc.StatusCode.FAILED_PRECONDITION,  # Not ready, failed, or already deleting.
                grpc.StatusCode.NOT_FOUND,
                grpc.StatusCode.DEADLINE_EXCEEDED,
                grpc.StatusCode.UNAVAILABLE,
            ):
                raise
            # A failed/lost stop acknowledgement must not strand partial resources.
            # The original delete-and-confirm path remains the fallback.
        try:
            after_stop = await asyncio.to_thread(self.client.get, name, workspace="default")
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                return {"cleanup": "DELETED"}
            raise
        if (
            after_stop.id != current.id
            or after_stop.labels.get("bridge-session")
            != (hashlib.sha256(self.session.encode()).hexdigest()[:32])
        ):
            return {"cleanup": "REMEDIATION_REQUIRED"}
        self.verify_release(after_stop)
        # Names are never reused for another session. Recheck after stop, then
        # wait for the immutable instance to disappear before a replacement writer.
        try:
            await asyncio.to_thread(
                self.client.delete, name, workspace="default", allow_missing=True
            )
        except grpc.RpcError as exc:
            if exc.code() not in (grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.UNAVAILABLE):
                raise
            # An acknowledgement can be lost while deletion continues. Reconcile
            # the immutable identity; never equate a timeout with success.

        await asyncio.to_thread(
            self.client.wait_deleted,
            name,
            workspace="default",
            expected_sandbox_id=current.id,
            timeout_seconds=60,
        )
        return {"cleanup": "DELETED"}

    async def close_all(self):
        self.closing = True
        if self.creations:
            await asyncio.shield(asyncio.gather(*self.creations, return_exceptions=True))
            self.creations = {task for task in self.creations if not task.done()}
        resources = await asyncio.to_thread(lambda: self.client.list(workspace="default").all())
        owner = hashlib.sha256(self.session.encode()).hexdigest()[:32]
        for ref in resources:
            if ref.labels.get("bridge-session") != owner:
                continue
            generation = int(ref.labels["bridge-generation"])
            self.configure_generation(generation)
            binding = {
                "session": self.session,
                "generation": generation,
                "name": ref.name,
                "sandbox_id": ref.id,
            }
            result = await self.close({"binding": binding})
            if result["cleanup"] != "DELETED":
                return result
        return {"cleanup": "DELETED"}
