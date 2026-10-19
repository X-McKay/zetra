from google.adk.agents import LlmAgent

from zetra import AgentContext, Tool

from .tools import tools_for


def create_agent(ctx: AgentContext) -> LlmAgent:
    inspect_workspace, write_summary = tools_for(ctx.workspace)
    return LlmAgent(
        name="workspace_reporter",
        instruction="Inspect the workspace, write a summary with write_summary, then confirm completion. Call each tool once in order.",
        model=ctx.model,
        tools=ctx.tools(
            Tool(inspect_workspace, "workspace_read"), Tool(write_summary, "workspace_write")
        ),
    )
