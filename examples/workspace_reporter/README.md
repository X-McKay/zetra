# workspace_reporter

A runnable Zetra agent using the nested project layout.

Run these commands with the candidate SDK environment from the Zetra checkout root:

```sh
uv run --locked zetra dev examples/workspace_reporter --input "Inspect and summarize."
uv run --locked zetra quality examples/workspace_reporter --explain
uv run --locked zetra quality examples/workspace_reporter
```

The quality gate executes five tool tests plus an outcome/content/trajectory/budget
case. Missing, skipped or failed required evidence fails the gate. Fixture execution
is trusted host code and measures deterministic behavior, not model accuracy or
sandbox isolation. Version-2 evaluation cases require this candidate SDK; the
example's dependency pin is retained until the prerequisite SDK commit is published.
Do not run these new commands from an environment restored to that older pin.
