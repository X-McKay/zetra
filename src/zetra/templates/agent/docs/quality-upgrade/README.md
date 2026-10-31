# Active quality contract

The starter pins SDK `c128bb199c64386e2fbda224e500ef40451eadcb`. Its V2 dataset
and quality contract are active in [evals/document_reader](../../evals/document_reader).
These are the sole maintained case and contract copies. This directory originally
held an inactive upgrade rehearsal for the earlier SDK pin; activation updated the
Git dependency and lockfile together and enabled the gate before wheel builds.

```sh
uv sync --locked
uv run --locked zetra quality . --agent document_reader --explain
uv run --locked zetra quality . --agent document_reader
just validate
```

The case checks the fixture's final `Completed.` marker, the actual
`read_document(document_id="guide")` call and at most three model/tool boundaries
(model, tool, model). The contract maps tool behavior to executed positive,
unapproved-ID and invalid-then-valid tests. The security tests establish the tool's
identifier allowlist, not host or sandbox isolation. Recovery means a later valid
tool call succeeds after rejection; it does not test durable recovery. No runtime
skills are declared. Fixture success establishes plumbing and bounded execution,
not guide-summary accuracy, prompt-injection resistance or live model quality.

The gate executes its mapped pytest selectors and fixture evaluation itself;
`--explain` checks coverage without executing that evidence. The normal validation
and release workflows execute the gate before building the application wheel.

For live evidence, create outcome assertions grounded in the guide's contents,
configure an explicit model endpoint using `--model-config`, and run `zetra dev`,
`eval --mode local` or `quality --mode local`. Keep credentials in the referenced
environment variable, not the config or report. The fixture marker case is not a
live summarization grader. Set `live_model_required` when live evidence becomes a
gate requirement. Local inference needs no Temporal or cluster connection.
Optional Linux `dev --temporal` runs actual Workflow/Activity boundaries; eval
and quality retain their existing fixture/local modes. See
[development](../development.md) for
supported commands, configuration and runtime boundaries.
