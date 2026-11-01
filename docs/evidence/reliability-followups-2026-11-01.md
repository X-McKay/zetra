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
