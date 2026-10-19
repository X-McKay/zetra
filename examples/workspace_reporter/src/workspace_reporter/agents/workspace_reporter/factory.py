from google.adk.agents import LlmAgent

from zetra import AgentContext, Tool

from .tools.workspace import tools_for


def create_agent(ctx: AgentContext) -> LlmAgent:
    inspect_workspace, write_summary = tools_for(ctx.workspace)
    return LlmAgent(
        name="workspace_reporter",
        instruction=ctx.instruction,
        model=ctx.model,
        tools=ctx.tools(
            Tool(inspect_workspace, "workspace_read"), Tool(write_summary, "workspace_write")
        ),
    )
