"""Real optional Temporal worker for trusted read-only agents.

Write-capable production workers are rejected until an independent approval,
revocation/fencing and idempotent receiver adapter is installed. The local
SQLite action demonstration is deliberately not enabled in this worker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from temporalio import activity
from temporalio.client import Client, TLSConfig
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from .evidence import fingerprint
from .gateway import GatewayMCPClient
from .loader import construct
from .manifest import ContractError, load
from .runtime import Denied, RuntimeDependencies
from .temporal_workflow import AgentWorkflow


class AgentActivities:
    def __init__(
        self,
        root: Path,
        knowledge: dict[str, str],
        gateway_client: GatewayMCPClient | None = None,
        release_id: str | None = None,
    ):
        self.root, self.knowledge = root, knowledge
        self.gateway_client = gateway_client
        self.release_id = release_id or fingerprint(root)
        self.manifest = load(root)
        if set(self.manifest.capabilities) - {"knowledge.read", "mcp.read_status"}:
            raise ContractError(
                "Only knowledge.read and explicitly configured mcp.read_status are supported by this read-only worker; write-capable or unqualified adapter start denied"
            )
        if "mcp.read_status" in self.manifest.capabilities and gateway_client is None:
            raise ContractError(
                "mcp.read_status worker requires explicitly configured gateway client"
            )

    @activity.defn(name="zetra.run_agent")
    def run_agent(self, request: dict) -> dict:
        manifest = self.manifest
        run_id = activity.info().workflow_run_id
        if not run_id:
            raise ApplicationError(
                "Only workflow-bound agent Activities are supported",
                type="ContractError",
                non_retryable=True,
            )
        deps = RuntimeDependencies(
            frozenset(manifest.capabilities),
            manifest.max_steps,
            manifest.max_seconds,
            manifest.max_cost_usd,
            self.root / ".zetra" / "STOP",
            knowledge=self.knowledge,
            gateway_client=self.gateway_client,
            release_id=self.release_id,
            run_id=run_id,
        )
        try:
            if not isinstance(request, dict):
                raise ContractError("Agent request must be an object")
            # A write adapter must use stable business-operation identity. This
            # activity is read-only; no misleading exactly-once claim is made.
            result = construct(self.root, deps).run(request)
            if not isinstance(result, dict):
                raise ContractError("Agent result must be an object")
            return result
        except (Denied, ContractError) as exc:
            raise ApplicationError(str(exc), type=type(exc).__name__, non_retryable=True) from exc


async def connect(args) -> Client:
    tls_args = (args.tls_ca, args.tls_cert, args.tls_key)
    if any(tls_args) and not all(tls_args):
        raise ContractError("TLS requires --tls-ca, --tls-cert and --tls-key")
    if not all(tls_args) and args.address.split(":")[0] not in {"localhost", "127.0.0.1", "[::1]"}:
        raise ContractError("Non-loopback Temporal connection requires explicit TLS credentials")
    tls = (
        TLSConfig(
            server_root_ca_cert=Path(args.tls_ca).read_bytes(),
            client_cert=Path(args.tls_cert).read_bytes(),
            client_private_key=Path(args.tls_key).read_bytes(),
        )
        if all(tls_args)
        else False
    )
    return await Client.connect(args.address, namespace=args.namespace, tls=tls)


async def work(args) -> None:
    root = Path(args.root).resolve()
    knowledge = (
        json.loads(Path(args.knowledge).read_text())
        if args.knowledge
        else {"zetra": "Zetra means ZEro-TRust Agents."}
    )
    if not isinstance(knowledge, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in knowledge.items()
    ):
        raise ContractError("Knowledge fixture must map strings to strings")
    gateway_client = None
    if args.gateway_endpoint:
        if not args.gateway_token_file:
            raise ContractError("Gateway requires an explicitly mounted --gateway-token-file")
        # The workload token is read on each request to permit short-lived token
        # rotation. Never pass bearer tokens in argv or write them to audit.
        gateway_client = GatewayMCPClient(
            args.gateway_endpoint,
            token_provider=lambda: Path(args.gateway_token_file).read_text().strip(),
            audit=lambda event: print(json.dumps(event), flush=True),
            allow_loopback_http=args.gateway_loopback_lab,
        )
    activities = AgentActivities(root, knowledge, gateway_client=gateway_client)
    client = await connect(args)
    with ThreadPoolExecutor(max_workers=4) as executor:
        worker = Worker(
            client,
            task_queue=args.task_queue,
            workflows=[AgentWorkflow],
            activities=[activities.run_agent],
            activity_executor=executor,
        )
        await worker.run()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Zetra read-only Temporal worker; no production write adapter enabled"
    )
    p.add_argument("--root", required=True)
    p.add_argument("--address", default="127.0.0.1:7233")
    p.add_argument("--namespace", default="default")
    p.add_argument("--task-queue", default="zetra-readonly")
    p.add_argument("--knowledge")
    p.add_argument("--gateway-endpoint")
    p.add_argument("--gateway-token-file")
    p.add_argument(
        "--gateway-loopback-lab",
        action="store_true",
        help="Permit HTTP only for a loopback lab endpoint",
    )
    for field in ("ca", "cert", "key"):
        p.add_argument(f"--tls-{field}")
    return p


if __name__ == "__main__":
    asyncio.run(work(parser().parse_args()))
