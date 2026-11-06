# Bounded budget conformance backend

This dedicated deterministic model qualifies the gateway → APS → Redis budget path
for spec T13/T13b. It does **not** qualify Qwen token accounting, vLLM context bounds,
production multi-completion behavior, or Redis crash durability.

`conformance-byte-v1` defines one token as one UTF-8 byte. Input usage is the sum of
all message role and content bytes, in order, with no hidden template/BOS/EOS tokens.
Output is exactly `max_tokens` ASCII `x` bytes. The response computes usage from these
actual input/output bytes. JSON envelope bytes are not part of this model's tokenizer.
The server enforces input <=200, output1..100 and total<=300; the catalogue entry
**for this backend only** is therefore `maxModelLen: 300`. Existing production
`llm-main: 131072` and `llm-fast: 32768` entries must remain unchanged.

The normal qualification request has role `user` (4 bytes), content46 ASCII `x`
bytes and max_tokens50: actual input50 + output50 =100 tokens. Requests use exactly
one completion. Unknown fields (including best_of and max_completion_tokens), n!=1,
streaming, nonzero temperature, oversized input/body and unsupported message shapes
are rejected. The optional `x-conformance-hold-ms` header is bounded0..10000; it delays
an admitted response, never changes usage or the bound. There is no control/reset API.

Required environment:

- `BUDGET_BACKEND_KEY_FILE`: mounted nonempty raw printable-ASCII backend key. Missing,
  unreadable or malformed configured keys fail startup. Keys never appear in logs.
- `BUDGET_BACKEND_AGENTS`: explicit comma-separated subset of
  `tenant-test/conformance-budget-day,tenant-test/conformance-budget-day-retry,tenant-test/conformance-budget-session`. The retry identity has the same enforced bounds and its own untouched ledger; it is never generated automatically.
- `BUDGET_BACKEND_ADDR`: optional listener, default `:8080`.

Only POST `/v1/chat/completions` performs inference. It requires the static backend
credential and well-formed APS-set agent/session/decision headers for an allowed
fixture. GET `/healthz` is unauthenticated. NetworkPolicy must allow the gateway to
this service and exclude direct sandbox/harness inference. The gateway test route
must preserve the normal JWT, agents-only, model-pin, APS authorization, backend-key
replacement and OTLP settlement path. Preserve the bounded hold header. Configure
its request limiter for30/minute with burst>=10 so the ten simultaneous attempts
reach APS; do not alter production routes or accept rate-limit429 as budget evidence.

Response logs contain session/decision/trace IDs, method/path/status, actual usage,
max_total_tokens300 and UTC received/response times. No request bodies, bearer
material or hashes of credentials are logged. `response_at` is sampled immediately
before writing the response, so the measured interval to APS settlement is
conservative. The qualification requires synchronized cluster clocks, rejects
negative intervals, and requires every observed interval<=5seconds; it does not
clamp timing or retry a failed call into a pass.

Build locally from the repository root with
`docker build --platform=linux/amd64 -t budget-backend:review platform/budget-backend`.
The Dockerfile cross-compiles static Go for the requested target architecture.
`go test ./...` and `go vet ./...` run in this directory and in platform CI.
