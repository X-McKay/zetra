"""The six-line assembly: an upstream agent built from the bundle."""

from __future__ import annotations

from typing import Any

from pydantic_ai import Agent
from pydantic_ai.tools import DeferredToolRequests

from . import context
from .bundle import Bundle
from .contracts import RunDeps
from .errors import CompileError
from .gateway import GatewayMCP
from .skills import Skills
from .tools import Bindings, build_toolset


def assemble(
    bundle: Bundle,
    bindings: Bindings,
    *,
    model: Any = None,
    durable: bool = False,
    gateway_stubs: dict[str, Any] | None = None,
    gateway_token: str | None = None,
) -> Agent[RunDeps, Any]:
    resolved = model if model is not None else bundle.project.resolve_model(bundle.spec.model or "")
    if resolved is None:
        raise CompileError(
            "ZT-SPEC-011",
            f"model policy {bundle.spec.model!r} is not resolved by zetra.toml [models]",
        )
    capabilities: list[Any] = []
    if durable:
        from datetime import timedelta

        from pydantic_ai.durable_exec.temporal import TemporalDurability
        from temporalio.common import RetryPolicy
        from temporalio.workflow import ActivityConfig

        # Every activity is bounded: PydanticAI's default is Temporal's unbounded retry, which
        # would spin forever on a gateway that refuses the worker. Writes are safe to retry
        # because the receiver returns the stored receipt for a repeated operation id.
        gateway_config: ActivityConfig = {
            "start_to_close_timeout": timedelta(seconds=30),
            "retry_policy": RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(seconds=10),
                maximum_attempts=5,
            ),
        }
        capabilities.append(
            TemporalDurability(
                activity_config={
                    "start_to_close_timeout": timedelta(seconds=60),
                    "retry_policy": RetryPolicy(
                        maximum_attempts=3, non_retryable_error_types=["Denied", "CompileError"]
                    ),
                },
                model_activity_config={
                    "start_to_close_timeout": timedelta(seconds=75),
                    "retry_policy": RetryPolicy(maximum_attempts=3),
                },
                toolset_activity_config={f"gateway-{s}": gateway_config for s in bundle.mcp},
            )
        )
    build_ctx = context.BuildContext(
        package_dir=bundle.package_dir,
        project=bundle.project,
        gateway_stubs=dict(gateway_stubs or {}),
        gateway_token=gateway_token,
    )
    with context.building(build_ctx):
        toolset = build_toolset(
            bundle.tools,
            input_type=bundle.input_type,
            bindings=bindings,
            reversible=bundle.project.reversible,
        )
        return Agent.from_spec(
            bundle.spec,
            name=bundle.name,
            model=resolved,
            deps_type=RunDeps,
            output_type=[*bundle.output_members, DeferredToolRequests],
            toolsets=[toolset],
            capabilities=capabilities,
            custom_capability_types=[Skills, GatewayMCP],
        )
