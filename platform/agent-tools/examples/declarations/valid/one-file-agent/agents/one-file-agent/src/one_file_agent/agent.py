from agent_runtime.report import report_tool


def create_agent(ctx) -> dict:
    return {"tools": [report_tool(ctx.workspace)]}
