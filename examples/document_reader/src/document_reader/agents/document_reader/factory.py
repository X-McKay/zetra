from google.adk.agents import LlmAgent

from zetra import AgentContext, Tool

from .tools.document_read import read_document


def create_agent(ctx: AgentContext) -> LlmAgent:
    return LlmAgent(
        name="document_reader",
        instruction=ctx.instruction,
        model=ctx.model,
        tools=ctx.tools(Tool(read_document, "workspace_read")),
    )
