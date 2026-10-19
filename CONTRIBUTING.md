# Contributing

Use Python 3.12 and the committed uv lockfile. Enter `nix develop`, then run
`just bootstrap` and `just check`. The root justfile is the command authority.
`just cluster-up` creates only the dedicated zetra-validation kind cluster.

New behavior requires a meaningful regression test. Preserve the full conformance
suite when changing the SDK. Run positive and negative sandbox probes, Temporal
history replay, recovery and live provider qualification after boundary changes.
Run benchmarks separately after correctness checks finish.

Keep the public API small. Agent factories and effect bodies must not be imported
by the external worker. Do not claim support for an ADK feature until its capture,
reconstruction and failure semantics have passed qualification.

Documentation describes the current implementation. Test-owned model recordings
are synthetic fixtures. Recorded evaluation and latency evidence must identify
its source/image/runtime scope; local evidence is unsigned.
