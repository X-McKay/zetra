import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.apps import App

from tests.fixtures.cases import FixtureCase, build_agent
from zetra._runtime.adk_compat import validate_profile
from zetra._runtime.capture import (
    BridgeModel,
    BridgeTool,
    TapeBridge,
    UnsupportedProfile,
    capture_boundary,
)


def factory(case):
    return lambda b: build_agent(case, BridgeModel(b), lambda f: BridgeTool(f, b))


@pytest.mark.parametrize(
    "field,value",
    [
        ("sub_agents", [LlmAgent(name="child")]),
        ("output_key", "result"),
        ("instruction", lambda ctx: "dynamic"),
        ("mode", "task"),
        ("retry_config", object()),
        ("code_executor", object()),
        ("planner", object()),
        ("on_tool_error_callback", lambda **kw: {"error": "hidden"}),
    ],
)
async def test_unsupported_agent_features_rejected(field, value):
    agent = factory(FixtureCase())(TapeBridge([]))
    # Deliberately bypass Pydantic validation to ensure admission checks the
    # object actually passed to Runner, not only its constructor inputs.
    object.__setattr__(agent, field, value)
    with pytest.raises(UnsupportedProfile):
        validate_profile(agent)


async def test_native_tools_and_provider_models_rejected():
    with pytest.raises(UnsupportedProfile):
        validate_profile(build_agent(FixtureCase()))


async def test_nondefault_app_and_run_features_rejected():
    agent = factory(FixtureCase())(TapeBridge([]))
    app = App(name="probe", root_agent=agent)
    object.__setattr__(app, "plugins", [object()])
    with pytest.raises(UnsupportedProfile):
        validate_profile(agent, app_config=app)
    with pytest.raises(UnsupportedProfile):
        validate_profile(agent, run_config=RunConfig(streaming_mode=StreamingMode.SSE))


async def test_callback_synthetic_result_rejected_before_model():
    case = FixtureCase()

    def build(bridge):
        agent = factory(case)(bridge)
        agent.before_model_callback = lambda **kwargs: case.responses()[0]
        return agent

    with pytest.raises(UnsupportedProfile):
        await capture_boundary(build, [])
    assert case.effects == []
