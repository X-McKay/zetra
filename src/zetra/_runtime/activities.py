"""Activity transport facade: heartbeat remote work and validate artifact receipts."""

import asyncio
import json
from contextlib import suppress

import grpc
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .artifacts import Position, Receipt
from .records import canonical_bytes, verify_operation
from .transport import enforce_payload_size
from .workspace import validate_snapshot


class Activities:
    def __init__(self, lifecycle, transport, tape):
        self.lifecycle = lifecycle
        self.transport = transport
        self.tape = tape

    async def heartbeating(self, operation):
        async def heartbeat():
            while True:
                activity.heartbeat()
                await asyncio.sleep(2)

        task = asyncio.create_task(heartbeat())
        try:
            return await operation
        except ValueError as exc:
            raise ApplicationError(str(exc), non_retryable=True) from exc
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def remote(self, action, payload):
        return await self.heartbeating(self.transport.call(action, payload))

    @activity.defn
    async def provision_session(self, start: dict) -> dict:
        # Compatibility for histories that scheduled the original combined Activity.
        binding = await self.heartbeating(self.lifecycle.provision(start))
        return await self.heartbeating(self.lifecycle.wait_ready(binding))

    @activity.defn
    async def rebind_session(self, command: dict) -> dict:
        binding = await self.heartbeating(self.lifecycle.replace(command))
        return await self.heartbeating(self.lifecycle.wait_ready(binding))

    @activity.defn
    async def create_session(self, command: dict) -> dict:
        operation = (
            self.lifecycle.replace(command)
            if command["binding"] is not None
            else self.lifecycle.provision(command["start"])
        )
        return await self.heartbeating(operation)

    @activity.defn
    async def wait_session_ready(self, binding: dict) -> dict:
        return await self.heartbeating(self.lifecycle.wait_ready(binding))

    @activity.defn
    async def advance_runner(self, command: dict) -> dict:
        result = await self.remote("advance", command)
        if result.get("status") == "operation":
            verify_operation(
                result["operation"],
                manifest=self.tape.manifest,
                index=command["position"]["cursor"],
            )
        elif result.get("status") != "done":
            raise ApplicationError("invalid advance response", non_retryable=True)
        if len(canonical_bytes(result)) > 262144:
            raise ApplicationError("response exceeds protocol limit", non_retryable=True)
        return enforce_payload_size(result, activity.payload_converter())

    async def execute(self, command):
        async def operation():
            result = await self.transport.call("execute", command)
            return await asyncio.to_thread(self.validate_receipt, command, result)

        return await self.heartbeating(operation())

    def validate_receipt(self, command, result):
        receipt = Receipt.model_validate(result)
        self.tape.load(receipt.position)
        previous = Position.model_validate(command["position"])
        op = verify_operation(
            command["operation"], manifest=self.tape.manifest, index=previous.cursor
        )
        self.tape.load(previous)
        node = json.loads(self.tape.store.get(receipt.position.head))
        if node["previous"] != (previous.head.model_dump() if previous.head else None):
            raise ApplicationError("candidate does not extend accepted head", non_retryable=True)
        entries = self.tape.load(receipt.position)
        if (
            receipt.previous != previous.head
            or entries[-1]["operation"] != op
            or receipt.position.cursor != previous.cursor + 1
        ):
            raise ApplicationError("invalid candidate chain", non_retryable=True)
        if receipt.position.workspace is None:
            raise ApplicationError("candidate has no workspace checkpoint", non_retryable=True)
        validate_snapshot(self.tape.store.get(receipt.position.workspace))
        return receipt.model_dump()

    @activity.defn
    async def execute_model(self, command: dict) -> dict:
        return await self.execute(command)

    @activity.defn
    async def execute_tool(self, command: dict) -> dict:
        return await self.execute(command)

    @activity.defn
    async def close_session(self, command: dict) -> dict:
        try:
            return await self.heartbeating(self.lifecycle.close(command))
        except grpc.RpcError as exc:
            if exc.code() in (
                grpc.StatusCode.PERMISSION_DENIED,
                grpc.StatusCode.UNAUTHENTICATED,
                grpc.StatusCode.INVALID_ARGUMENT,
                grpc.StatusCode.FAILED_PRECONDITION,
                grpc.StatusCode.UNIMPLEMENTED,
            ):
                raise ApplicationError(
                    f"OpenShell cleanup rejected: {exc.code().name}", non_retryable=True
                ) from exc
            raise

    def registered(self):
        return [
            self.provision_session,
            self.rebind_session,
            self.create_session,
            self.wait_session_ready,
            self.advance_runner,
            self.execute_model,
            self.execute_tool,
            self.close_session,
        ]
