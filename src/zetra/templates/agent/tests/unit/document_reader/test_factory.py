from google.adk.agents import LlmAgent
from zetra.agent import AgentContext

from agent_package.agents.document_reader.factory import create_agent


def test_factory_uses_context_model_tools_and_packaged_instruction(tmp_path):
    context = AgentContext(None, workspace=tmp_path, model="fixture", instruction="Read the guide.")
    agent = context.seal(create_agent(context))
    assert isinstance(agent, LlmAgent)
    assert agent.model is context.model
    assert agent.instruction == "Read the guide."
    assert [tool.name for tool in agent.tools] == ["read_document"]
    assert set(context.executors()) == {"read_document"}
