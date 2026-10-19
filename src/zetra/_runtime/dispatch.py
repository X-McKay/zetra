"""Volatile per-session execution deduplication, never durable progress authority."""

import asyncio
import json
from contextlib import nullcontext
from types import MappingProxyType

from .records import canonical_bytes, verify_operation


class KnownError(Exception):
    """Explicit business result; post-error workspace must still be captured."""

    def __init__(self, value):
        self.value = value
        super().__init__("known business error")


class OutcomeUnknown(RuntimeError):
    pass


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze(v) for v in value)
    return value


class Dispatcher:
    def __init__(self, registry, *, manifest, workspace=None):
        self.registry = MappingProxyType(dict(registry))
        self.manifest = manifest
        self.tasks = {}
        self.workspace = workspace
        self.checkpoints = {}

    async def execute(self, operation, attempt_id):
        operation = verify_operation(operation, manifest=self.manifest)
        key = operation["operation_id"]
        identity = canonical_bytes(operation)
        if key in self.tasks:
            old_identity, old_attempt, task = self.tasks[key]
            if old_identity != identity or old_attempt != attempt_id:
                raise ValueError("conflicting execution delivery")
        else:
            if any(not item[2].done() for item in self.tasks.values()):
                raise ValueError("another operation is still executing")
            if len(self.tasks) >= 64:
                raise ValueError("session execution limit")
            executor = self.registry[operation["executor"]]

            async def run():
                try:
                    payload = operation["request"]
                    with self.workspace.writer() if self.workspace else nullcontext():
                        try:
                            value = await executor(
                                freeze(payload.get("arguments", payload)),
                                freeze(payload.get("state", {})),
                                key,
                            )
                            status = "succeeded"
                        except KnownError as exc:
                            value = exc.value
                            status = "known_error"
                    if self.workspace:
                        self.checkpoints[key] = self.workspace.capture()
                    return {"status": status, "value": json.loads(canonical_bytes(value))}
                except Exception as exc:
                    # A body exception cannot establish whether an effect occurred.
                    raise OutcomeUnknown(f"operation outcome unknown: {key}") from exc

            task = asyncio.create_task(run())
            self.tasks[key] = (identity, attempt_id, task)
        result = await asyncio.shield(task)
        return json.loads(canonical_bytes(result))

    def checkpoint(self, operation):
        return self.checkpoints[operation["operation_id"]]

    async def close(self):
        tasks = [item[2] for item in self.tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
