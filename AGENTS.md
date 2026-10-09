# Zetra contributor instructions

Read `CONTRIBUTING.md`, `SECURITY.md` and the relevant package before changing code. Use `nix develop` or the pinned mise environment, `uv.lock` and the `justfile`. `just check` is the standard local gate; run affected live qualifications separately when a boundary changes.

The editable playbooks are `docs/chapters/*.html`, `docs/research/source-review.html` and `docs/assets/`. Rebuild committed standalone HTML with `just docs`. Keep examples, CLI behavior, schema and prose consistent. The target enterprise architecture is broader than the implemented v0.1 toolkit: distinguish proposals, candidate artifacts, discovery, tested enforcement and approved production operation.

Never edit recorded evidence to turn a failure into a pass. Re-run the applicable probe; preserve meaningful initial failures and record the actual compatibility tuple. Hashes and local timestamps do not create trusted signatures. Profiling observations never grant permissions, and denied observations must stay outside permission candidates.

Keep kubeconfigs, private certificates, keys and tool downloads in ignored local paths. All disposable lab mutations use the explicit `kind-zetra-validation` context and dedicated kubeconfig. Do not switch to a shared cluster or change a default runtime connection implicitly. Follow the user's authorization when deciding the scope of deployments, cleanup or external publication.

The optional reviewed workflow drafts live in `skills/`; installation instructions and test limits are in `docs/research/skills-guide.md`. A skill guides work but grants no authority. Report behavior tested, evidence paths and material gaps. Do not describe this initial toolkit as production-qualified.
