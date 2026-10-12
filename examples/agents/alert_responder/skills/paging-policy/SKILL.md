---
name: paging-policy
description: House rules for paging the on-call engineer. Load before deciding whether an alert warrants a page.
---
Page when all of the following hold: severity is critical, the service status is
degraded or down, and no deploy of that service finished in the last 15 minutes
(a fresh deploy is handled by the deploy owner). Never page for info severity.
When you page, the summary names the service, the symptom and the first thing
to check.
