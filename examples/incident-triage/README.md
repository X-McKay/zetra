# Incident triage reference

One continuous deterministic run reads an approved checkout runbook and a fixed status tool, proposes an exact ticket payload, obtains local controller approval, creates one ticket, and returns the same receipt on retry. The demo denies approval reuse and a later invocation after its STOP marker.

From the repository root, after `just bootstrap`:

```sh
just incident-triage

.venv/bin/zetra check examples/incident-triage
.venv/bin/zetra eval examples/incident-triage \
  --output build/incident-triage-evidence.json
.venv/bin/zetra check examples/incident-triage \
  --evidence build/incident-triage-evidence.json
```

The eight scenario gates cover strict contracts, untrusted data, capabilities, gateway client requirements, per-instance budgets, stop, exact approval binding and retry/idempotency. The internal-data, supervised write manifest produces a T2 risk candidate requiring accountable review.

The MCP wire fixture makes **no network connections** and performs **no real JWT verification**. The receiver writes only a temporary SQLite database, removed after the demo. There are no model calls or production tickets. Local actor/run/release checks and budgets are cooperative; the receiver does not authenticate a production principal. Signed authorization, tenant/resource/policy binding, remote idempotency, aggregate accounting and distributed revocation remain production adapter requirements. The existing Temporal worker rejects this write-capable manifest.

See the [step-by-step walkthrough](../../docs/use-case-walkthrough.html) for the exact input, proposal, digest, trace, failure matrix, release pipeline and proposed durable approval/write bridge.
