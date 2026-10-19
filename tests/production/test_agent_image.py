"""Guard the packaged runtime recipe used by ordinary agent builds."""

from pathlib import Path

RECIPE = Path(__file__).resolve().parents[2] / "src/zetra/agent.Dockerfile"


def test_agent_build_tools_are_confined_to_builder():
    recipe = RECIPE.read_text()
    stages = recipe.split("\nFROM ")
    assert len(stages) >= 3, "agent dependencies need a separate builder stage"
    builder, runtime = stages[-2:]
    assert " git" in builder
    assert "uv sync --locked --no-dev --no-editable --compile-bytecode" in builder
    assert " git" not in runtime
    assert "/usr/local/bin/uv" not in runtime
    assert "COPY --from=builder /app /app" in runtime
    assert 'PATH="/app/.venv/bin:$PATH"' in runtime
    assert "USER agent" in runtime
    assert "--uid 1500" in runtime
    assert 'CMD ["/app/.venv/bin/python"' in runtime


def test_ordinary_agent_image_has_no_qualification_canaries():
    recipe = RECIPE.read_text()
    assert "/private-canary" not in recipe
    assert "/restricted-write" not in recipe
