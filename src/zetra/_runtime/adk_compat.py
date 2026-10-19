"""Pinned ADK lifecycle seams; contains no Temporal or agent-repository imports."""

import random
import uuid
from contextlib import contextmanager

from google.adk.agents.run_config import RunConfig
from google.adk.platform import reset_random_provider, set_random_provider
from google.adk.platform.time import reset_time_provider, set_time_provider
from google.adk.platform.uuid import reset_id_provider, set_id_provider
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .records import encode_model


@contextmanager
def deterministic_runtime(invocation="probe-invocation"):
    counter = iter(range(1000000))
    rng = random.Random(42)
    set_time_provider(lambda: 1700000000.0)
    set_id_provider(lambda: str(uuid.uuid5(uuid.NAMESPACE_URL, f"{invocation}:{next(counter)}")))
    set_random_provider(lambda: rng)
    try:
        yield
    finally:
        reset_time_provider()
        reset_id_provider()
        reset_random_provider()


async def run_agent(factory, message="Inspect this repository", *, invocation="probe-invocation"):
    """Drain ADK to exhaustion, closing its iterator and resources on cancellation."""
    with deterministic_runtime(invocation):
        agent = factory()
        sessions = InMemorySessionService()
        await sessions.create_session(app_name="probe", user_id="user", session_id="session")
        runner = Runner(agent=agent, app_name="probe", session_service=sessions)
        iterator = runner.run_async(
            user_id="user",
            session_id="session",
            new_message=types.Content(role="user", parts=[types.Part(text=message)]),
            run_config=RunConfig(max_llm_calls=64),
        )
        text = ""
        try:
            async for event in iterator:
                if event.is_final_response() and event.content:
                    text = "".join(p.text or "" for p in event.content.parts or [])
            session = await sessions.get_session(
                app_name="probe", user_id="user", session_id="session"
            )
            return {"session": encode_model(session), "final_text": text}
        finally:
            await iterator.aclose()
            await runner.close()


def validate_profile(agent, app_config=None, run_config=None):
    from google.adk.agents import LlmAgent
    from google.adk.agents.run_config import RunConfig
    from google.adk.apps import App

    from .capture import BridgeModel, BridgeTool, UnsupportedProfile

    if type(agent) is not LlmAgent or type(agent.model) is not BridgeModel:
        raise UnsupportedProfile("agent and model must use the admitted bridge")
    allowed = {
        "name",
        "description",
        "model",
        "instruction",
        "tools",
        "before_agent_callback",
        "after_agent_callback",
        "before_model_callback",
        "after_model_callback",
        "before_tool_callback",
        "after_tool_callback",
    }
    baseline = LlmAgent(name="baseline")
    for name in type(agent).model_fields:
        if name not in allowed and getattr(agent, name) != getattr(baseline, name):
            raise UnsupportedProfile(f"unsupported agent feature: {name}")
    if not isinstance(agent.instruction, str) or not all(
        type(t) is BridgeTool for t in agent.tools
    ):
        raise UnsupportedProfile("dynamic instructions or unwrapped tools")
    if app_config is not None:
        if type(app_config) is not App or app_config.root_agent is not agent:
            raise UnsupportedProfile("unbound app")
        if any(
            getattr(app_config, key)
            for key in (
                "plugins",
                "events_compaction_config",
                "context_cache_config",
                "resumability_config",
            )
        ):
            raise UnsupportedProfile("unsupported App feature")
    if run_config is not None and (
        type(run_config) is not RunConfig or run_config != RunConfig(max_llm_calls=64)
    ):
        raise UnsupportedProfile("unsupported RunConfig feature")
