# Reliability follow-ups

The separately authorized fixed study on SDK `78bae4f`, agents `9fad0e0` and
template `51962d4` passed all 20 smoke cases, then recorded 748 passing trials,
five trajectory failures, two unknown model outcomes and 45 unrun trials of 800
planned. Starter passed 240/240. Document assistant recorded 310 passes, five
failures, one error and four unrun; investigator recorded 198 passes, one error
and 41 unrun. Both incomplete suites stopped because usage was unavailable.
These original artifacts and results remain unchanged in the draft PR records.

Five correct document-assistant output files lacked the required preceding
document-read/grounding-skill result. Its write tool previously independently
read the packaged source, so output validation alone could not enforce the
trajectory contract. The new guard requires accepted `read_document` evidence
for the approved source and skill before touching the file. Refusal is an
explicit tool result; the model may read and try writing afterward, but the
original strict trajectory assertion still judges the whole invocation.

`AgentContext.prior_tool_results` receives the caller-selected accepted tape from
the shared runtime. Immutable successful tool results survive full runtime
reconstruction; a volatile flag, candidate receipt or existing workspace file
does not authorize a write. Tools still check business-result content, since
operation success alone does not imply business success. Unit tests inject
explicitly accepted history; they do not establish sandbox isolation.

The two unknown model outcomes cannot be retrospectively diagnosed from the old
compact reports. Controlled loopback HTTP and malformed-provider tests reproduced
the loss of cause metadata before the correction. New allowlisted local diagnostics
identify supported failure reasons, optional HTTP status and the boundary index.
They omit payloads, credentials and arbitrary exception text. The converter can
wrap connection failures and malformed JSON as synthetic HTTP 500s; classification
inspects known underlying exception types rather than inferring a server failure
from that wrapper. HTTP status requires an underlying OpenAI HTTP exception.
No model retry, unknown-usage substitution or grading relaxation is introduced.
Historical schema-3 evidence without diagnostics retains its serialized identity.

Validation results and immutable dependency revisions are recorded in
[SDK #19](https://github.com/X-McKay/zetra/pull/19),
[agents #5](https://github.com/X-McKay/zetra-agents/pull/5) and
[template #6](https://github.com/X-McKay/zetra-template/pull/6).
No external inference or new reliability study is part of these local fixes.
The previous failures remain failures. Production qualification remains paused.

The SDK's full local gate passed 590 tests with local Temporal enabled and 46
cluster tests deselected; lint, format, lock, docs, examples and quality checks
passed. The final classifier and compact-report suite separately passed 75 tests,
including the additional malformed-JSON regression. All six smoke/reliability
reports from the preceding study validated without modification. Native local
Temporal checks passed 34 tests. Installed runtime validation passed 92 tests;
canonical and freshly rendered starters passed 30 tests each plus quality and
wheel checks, and renderer tests passed 13. Bundled scaffold/eval/CLI validation
passed 68. Existing ADK experimental and LiteLLM logging warnings remain.
Independent review found no issues in accepted-history recovery, diagnostic
privacy, both converter-classification corrections or the scope documentation.

The SDK prerequisite is `a74b3f6e042353fb9d6d5c36942af4d988c7cd47`, and runtime
prerequisite is `eb444ea3473452042760458658df94f5abdf7d95`. Preserve these reachable
commits when integrating the stacked drafts. Lock refresh used Nix's uv 0.12.22
and Python 3.12.14; refreshed lock format is revision 5. CI retains its existing
uv 0.13.1 pin. No package versions changed in this dependency refresh.
