from google.adk.agents import LlmAgent

from zetra import AgentContext, Tool

from .tools import read_document


def create_agent(ctx: AgentContext) -> LlmAgent:
    return LlmAgent(
        name="document_reader",
        instruction="Read the approved guide once and answer using it.",
        model=ctx.model,
        tools=ctx.tools(Tool(read_document, "workspace_read")),
    )
