"""Fake Kubernetes, APS and exec websocket for activity tests."""

from __future__ import annotations

import json
import time
import uuid

import yaml
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from runner import activities as act

TENANT = "tenant-a"
SESSION = "6f1d2c3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"
RELEASE = "investigator-2026-10-29-a93557ad"

NETPOLS_YAML = """
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: default-deny-all, namespace: tenant-a}
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: sandbox-egress-gateway-only, namespace: tenant-a}
spec:
  podSelector:
    matchLabels: {agentplatform.io/sandbox: "true"}
  policyTypes: [Egress]
  egress:
    - to:
        - namespaceSelector:
            matchLabels: {kubernetes.io/metadata.name: ai-gateway}
          podSelector:
            matchLabels: {app.kubernetes.io/name: agents}
      ports:
        - {protocol: TCP, port: 80}
        - {protocol: TCP, port: 443}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: sandbox-ingress-none, namespace: tenant-a}
spec:
  podSelector:
    matchLabels: {agentplatform.io/sandbox: "true"}
  policyTypes: [Ingress]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: tenant-no-other-egress, namespace: tenant-a}
spec:
  podSelector:
    matchExpressions:
      - {key: agentplatform.io/sandbox, operator: DoesNotExist}
  policyTypes: [Egress]
"""

ALLOW_ALL_EGRESS = {
    "apiVersion": "networking.k8s.io/v1",
    "kind": "NetworkPolicy",
    "metadata": {"name": "allow-all-egress", "namespace": TENANT},
    "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{}]},
}

POD_TEMPLATE = f"""
apiVersion: v1
kind: Pod
metadata:
  name: placeholder
  namespace: {TENANT}
  labels:
    agentplatform.io/sandbox: "true"
    agentplatform.io/agent: investigator
    agentplatform.io/tenant: {TENANT}
    agentplatform.io/release: {RELEASE}
    agentplatform.io/session: placeholder
    agentplatform.io/generation: "0"
  annotations:
    agentplatform.io/policy-digest: sha256:9c1e
spec:
  runtimeClassName: kata-qemu-runtime-rs
  serviceAccountName: agent-investigator
  containers:
    - name: agent
      image: registry.example.com/agents/investigator@sha256:a93557ad
      env:
        - {{name: AGENT_CLIENT_ID, value: agent-tenant-a-investigator}}
        - {{name: AGENT_SESSION_ID, value: placeholder}}
"""


class _Resp:
    def __init__(self, data):
        self.data = json.dumps(data)


def model(data, kind: str):
    return client.ApiClient().deserialize(_Resp(data), kind)


def netpols(extra: list[dict] | None = None):
    docs = [d for d in yaml.safe_load_all(NETPOLS_YAML)] + (extra or [])
    return model({"items": docs}, "V1NetworkPolicyList").items


def controller_digest() -> str:
    return act.netpol_digest(netpols())[1]


class FakeCore:
    def __init__(self, digest: str | None = None):
        self.pods: dict[str, client.V1Pod] = {}
        self.finalized: set[str] = set()  # pods whose deletion never completes
        self.deleted: list[str] = []
        self.created: list[dict] = []
        self.reject_create: int | None = None
        self.phase = "Running"
        self.annotations = {"agentplatform.io/netpol-digest": digest or controller_digest()}
        self.template = POD_TEMPLATE

    def read_namespace(self, ns):
        return client.V1Namespace(
            metadata=client.V1ObjectMeta(name=ns, annotations=self.annotations)
        )

    def read_namespaced_config_map(self, name, ns):
        if name != f"podtemplate-{RELEASE}":
            raise ApiException(status=404)
        return client.V1ConfigMap(data={"pod.yaml": self.template})

    def create_namespaced_pod(self, ns, body):
        if self.reject_create:
            raise ApiException(status=self.reject_create, reason="admission webhook denied")
        name = body["metadata"]["name"]
        if name in self.pods:
            raise ApiException(status=409, reason="AlreadyExists")
        self.created.append(body)
        pod = model(
            {
                **body,
                "metadata": {**body["metadata"], "uid": str(uuid.uuid4())},
                "status": {"phase": self.phase},
            },
            "V1Pod",
        )
        self.pods[name] = pod
        return pod

    def read_namespaced_pod(self, name, ns):
        if name not in self.pods:
            raise ApiException(status=404)
        return self.pods[name]

    def delete_namespaced_pod(self, name, ns, grace_period_seconds=None):
        self.deleted.append(name)
        if name not in self.pods:
            raise ApiException(status=404)
        if name not in self.finalized:
            del self.pods[name]

    def list_namespaced_pod(self, ns, label_selector=None):
        return client.V1PodList(items=list(self.pods.values()))

    def connect_get_namespaced_pod_exec(self, *a, **kw):  # replaced by the fake stream
        raise AssertionError("not called directly")


class FakeNet:
    def __init__(self, extra=None):
        self.extra = extra

    def list_namespaced_network_policy(self, ns):
        return client.V1NetworkPolicyList(items=netpols(self.extra))


class FakeCustom:
    def __init__(self, status: dict | None = None, agent: str = "investigator"):
        self.status = {
            "ready": True,
            "identityMode": "agent",
            "delegationRequired": False,
            **(status or {}),
        }
        self.agent = agent

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        if name != RELEASE:
            raise ApiException(status=404)
        return {"spec": {"agent": self.agent}, "status": self.status}


class FakeAPS:
    def __init__(self, grants: dict | None = None, sessions: list | None = None):
        self.grants = grants or {}
        self.sessions = sessions or []
        self.calls: list[tuple[str, str]] = []

    def get(self, path):
        from temporalio.exceptions import ApplicationError

        self.calls.append(("GET", path))
        if path.startswith("/v1/sessions"):
            return {"sessions": self.sessions}
        handle = path.rsplit("/", 1)[1]
        if handle not in self.grants:
            raise ApplicationError("APS GET refused with 404", type="Forbidden", non_retryable=True)
        return self.grants[handle]

    def put(self, path, body):
        self.calls.append(("PUT", path))

    def post(self, path, body):
        self.calls.append(("POST", path))

    def delete(self, path):
        self.calls.append(("DELETE", path))


class FakeWS:
    """A websocket that yields scripted stdout chunks and then an exit code."""

    def __init__(
        self,
        chunks=(),
        rc: int | None = 0,
        never_close: bool = False,
        fail_after: int | None = None,
    ):
        self.chunks = iter(chunks)
        self.rc = rc
        self.never_close = never_close
        self.fail_after = fail_after
        self.open = True
        self.buffer = b""
        self.pulled = 0
        self.frames = 0
        self.stdin = bytearray()
        self.closed = False

    def is_open(self):
        return self.open

    def update(self, timeout=0):
        if not self.open:
            return
        if self.fail_after is not None and self.frames >= self.fail_after:
            raise ConnectionResetError("websocket dropped")
        chunk = next(self.chunks, None)
        if chunk is None:
            if self.never_close:
                time.sleep(min(timeout or 0, 0.02))
            else:
                self.open = False
            return
        self.frames += 1
        self.pulled += len(chunk)
        self.buffer += chunk

    def peek_stdout(self, timeout=0):
        return self.buffer

    def read_stdout(self, timeout=None):
        out, self.buffer = self.buffer, b""
        return out

    def peek_stderr(self, timeout=0):
        return b""

    def read_stderr(self, timeout=None):
        return b""

    def write_stdin(self, data):
        self.stdin.extend(data)

    @property
    def returncode(self):
        if self.open:
            return None
        if self.rc is None:
            raise TypeError("no status on the error channel")
        return self.rc

    def close(self):
        self.closed = True
        self.open = False


class FakeStream:
    """Stands in for kubernetes.stream.stream; each exec takes the next scripted websocket."""

    def __init__(self, *sockets: FakeWS, refuse: bool = False):
        self.sockets = list(sockets)
        self.commands: list[list[str]] = []
        self.kwargs: list[dict] = []
        self.used: list[FakeWS] = []
        self.refuse = refuse

    def __call__(self, api_method, name, ns, **kw):
        if self.refuse:
            raise ApiException(status=0, reason="handshake failed")
        self.commands.append(kw["command"])
        self.kwargs.append(kw)
        ws = self.sockets.pop(0)
        self.used.append(ws)
        return ws


def boundary_doc(n: int, done: bool = False, **over) -> bytes:
    doc = {
        "boundary": n,
        "kind": "model",
        "done": done,
        "tool": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "summary": "listed pods",
    }
    doc.update(over)
    return json.dumps(doc).encode()
