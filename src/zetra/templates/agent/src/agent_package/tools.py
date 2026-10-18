def tools_for(workspace):
    async def inspect_workspace(path: str) -> dict:
        """List the workspace; path must be a dot."""
        if path != ".":
            raise ValueError("only the workspace root is allowed")
        return {"files": sorted(p.name for p in workspace.iterdir())}

    async def write_summary(content: str) -> dict:
        """Write a summary in the session workspace."""
        (workspace / "summary.md").write_text(content)
        return {"path": "summary.md"}

    return inspect_workspace, write_summary
