# Prepared local quality upgrade

These files are a prepared upgrade, outside the active `evals/document_reader`
dataset. The pinned SDK does not supply these V2 assertions or `zetra quality`.
Keep normal validation and CI on their supported commands until a new SDK revision
is published and reachable, its exact Git pin and lockfile are updated together,
and the rendered project passes validation. Then review and activate the prepared
case and contract before enabling `zetra quality` in CI. A local SDK checkout is
candidate evidence, not a published dependency or permission to change the pin.

The case checks the fixture's final `Completed.` marker, the actual
`read_document(document_id="guide")` call and at most three model/tool boundaries
(model, tool, model). The contract maps tool behavior to executed positive,
unapproved-ID and invalid-then-valid tests. The security tests establish the tool's
identifier allowlist, not host or sandbox isolation. Recovery means a later valid
tool call succeeds after rejection; it does not test durable recovery. No runtime
skills are declared. Fixture success establishes plumbing and bounded execution,
not guide-summary accuracy, prompt-injection resistance or live model quality.

Rehearse only in a disposable rendered project. From the canonical template
repository, set `CANDIDATE_SDK` to a reviewed candidate SDK checkout whose locked
Python environment is already installed:

```sh
REHEARSAL_PARENT=$(mktemp -d)
python scripts/render.py "$REHEARSAL_PARENT/project" --name quality-rehearsal
cd "$REHEARSAL_PARENT/project"
cp docs/quality-upgrade/cases.jsonl evals/document_reader/cases.jsonl
cp docs/quality-upgrade/quality.json evals/document_reader/quality.json
export PYTHONPATH="$PWD/src:$CANDIDATE_SDK/src"
"$CANDIDATE_SDK/.venv/bin/python" -m pytest
"$CANDIDATE_SDK/.venv/bin/python" -m zetra quality . --agent document_reader --explain
"$CANDIDATE_SDK/.venv/bin/python" -m zetra eval . --agent document_reader --mode fixture
"$CANDIDATE_SDK/.venv/bin/python" -m zetra quality . --agent document_reader --mode fixture
```

This deliberately runs the candidate interpreter and SDK source rather than
claiming that `uv run --locked` uses an unpublished revision. Do not copy the
rehearsal's eval replacement back into the active pinned starter yet.

For live evidence, create separate outcome assertions grounded in the guide's
contents, configure an explicit model endpoint with the candidate SDK's supported
`--model-config` option, and run `zetra dev`, `eval --mode local` or
`quality --mode local`. Keep credentials in the referenced environment variable,
not the config or report. The fixture marker case is not a live summarization
grader. Set `live_model_required` when live evidence becomes a gate requirement.
The local runner needs no Temporal or cluster connection; local Temporal remains
a separate, explicitly configured milestone.
