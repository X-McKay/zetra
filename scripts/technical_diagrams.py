"""Accessible, offline diagrams for the implementation and incident walkthroughs."""

from html import escape

PALETTE = {
    "teal": ("#e3f5f0", "#087f85"),
    "blue": ("#e6eef9", "#37629a"),
    "orange": ("#fff0e5", "#bd5e30"),
    "ink": ("#eaf0f2", "#1d4258"),
}


def label(x, y, text, size=12, color="#4e6473", weight=400, anchor="start"):
    return f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{weight}" text-anchor="{anchor}">{escape(text)}</text>'


def box(x, y, title, lines, tone="teal", width=316, height=112):
    bg, stroke = PALETTE[tone]
    result = f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="9" fill="{bg}" stroke="{stroke}" stroke-width="1.4"/>'
    result += label(x + 16, y + 27, title, 15, stroke, 700)
    for i, line in enumerate(lines):
        result += label(x + 16, y + 50 + i * 19, line)
    return result


def edge(points, dashed=False, tone="teal"):
    stroke = PALETTE[tone][1]
    path = "M" + " L".join(f"{x},{y}" for x, y in points)
    dash = ' stroke-dasharray="5 5"' if dashed else ""
    return f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="1.6" marker-end="url(#technical-arrow)"{dash}/>'


def band(y, title, detail, tone="ink", height=64):
    return box(24, y, title, [detail], tone, width=1032, height=height)


def figure(key, title, body, height, caption):
    marker = "technical-" + key
    body = body.replace("url(#technical-arrow)", f"url(#{marker})")
    return (
        f'<figure class="technical-diagram" data-diagram-key="{key}"><p class="diagram-title">{escape(title)}</p>'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1080 {height}" role="img" aria-label="{escape(title)}">'
        f'<defs><marker id="{marker}" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8" fill="#6a939d"/></marker></defs>'
        f"{body}</svg><figcaption>{escape(caption)}</figcaption></figure>"
    )


def technical_diagram(key):
    if key == "integration-control-plane":
        body = label(
            24, 28, "EVERY TRANSITION HAS AN OWNER, AN ARTIFACT AND A GATE", 12, weight=700
        )
        stages = [
            (
                24,
                56,
                "1 / Developer package",
                [
                    "Typed factory + agent.yaml",
                    "Declared intent, evals, ownership",
                    "No vendor wiring inside agent code",
                ],
                "teal",
            ),
            (
                382,
                56,
                "2 / Trusted CI evaluation",
                [
                    "Check → eval → profile candidate",
                    "Source + manifest + evaluator digests",
                    "Denied observations stay denied",
                ],
                "blue",
            ),
            (
                740,
                56,
                "3 / Independent policy review",
                [
                    "Intent ∩ org limits ∩ approved rights",
                    "Versioned adapter capability checks",
                    "No trace grants its own authority",
                ],
                "orange",
            ),
            (
                740,
                246,
                "4 / Withheld deployment",
                [
                    "Immutable image + replicas: 0",
                    "Effective policy / identity receipts",
                    "Positive and bypass-negative probes",
                ],
                "blue",
            ),
            (
                382,
                246,
                "5 / Release activation",
                [
                    "Owner approves evidence + scope",
                    "Target: signed release, epoch, lease",
                    "Qualification required before start",
                ],
                "orange",
            ),
            (
                24,
                246,
                "6 / Accountable operation",
                [
                    "Run / operation receipts + alerts",
                    "Risk changes trigger requalification",
                    "Independent revocation + recovery",
                ],
                "teal",
            ),
        ]
        for x, y, title, lines, tone in stages:
            body += box(x, y, title, lines, tone)
        body += edge([(340, 112), (376, 112)]) + edge([(698, 112), (734, 112)])
        body += edge([(898, 168), (898, 240)])
        body += edge([(740, 302), (704, 302)]) + edge([(382, 302), (346, 302)])
        body += edge([(182, 246), (182, 174)], True)
        body += label(27, 215, "Feedback: new evidence", 11)
        body += band(
            412,
            "Implementation boundary",
            "Working CLI + individual live fixtures exist. Signed compilation, admission and full production activation remain targets.",
        )
        return figure(
            key,
            "09 / From a portable package to an accountable release",
            body,
            496,
            "Solid arrows are lifecycle transitions; the dashed arrow requests re-evaluation. A successful build cannot substitute for an approved and qualified release.",
        )
    if key == "integration-runtime-hops":
        body = label(
            24,
            28,
            "PROPOSED PRODUCTION COMPOSITION • INDIVIDUAL BOUNDARIES HAVE LOCAL EVIDENCE",
            12,
            weight=700,
        )
        body += box(
            24,
            62,
            "Temporal control worker",
            [
                "Deterministic workflow coordination",
                "Activities hold external I/O",
                "Worker identity ≠ agent identity",
            ],
            "blue",
        )
        body += box(
            382,
            62,
            "Sandbox execution bridge",
            [
                "Proposed adapter, not implemented",
                "Start supervisor-managed process",
                "Verify policy receipt before task",
            ],
            "orange",
        )
        body += box(
            740,
            62,
            "OpenShell managed boundary",
            [
                "Workload + separate supervisor pod",
                "Boundary Service + network fence",
                "Short-lived scoped identity",
            ],
            "teal",
        )
        body += edge([(340, 118), (376, 118)]) + edge([(698, 118), (734, 118)], True)
        body += box(
            740,
            282,
            "AgentGateway",
            [
                "Validate JWT + route / tool policy",
                "No arbitrary upstream credentials",
                "Bind identity to approved backend",
            ],
            "teal",
        )
        body += box(
            382,
            282,
            "Resource / effect receiver",
            [
                "Authorize tenant + resource + action",
                "Grant, payload digest, epoch check",
                "Atomic effect + idempotent receipt",
            ],
            "orange",
        )
        body += box(
            24,
            282,
            "Identity / secret authority",
            [
                "Issue bounded workload credentials",
                "Gateway owns backend credential",
                "Receiver owns business permission",
            ],
            "blue",
        )
        body += edge([(898, 174), (898, 276)]) + label(908, 232, "TLS + workload identity", 11)
        body += edge([(740, 338), (704, 338)])
        body += edge([(340, 338), (376, 338)], True)
        body += label(422, 236, "Backend remains an authorization boundary", 12, color="#bd5e30")
        body += band(
            448,
            "Cilium / Kubernetes network boundary",
            "Constrain peer + port paths. Gateway-only routing requires tested endpoints, DNS, host-network and bypass rules.",
            "blue",
        )
        body += band(
            532,
            "Tetragon / eBPF node boundary",
            "Observe selected kernel activity and enforce qualified hooks. Kernel events cannot prove semantic business approval.",
            "teal",
        )
        body += band(
            616,
            "OpenTelemetry + catalog correlation",
            "Release → policy digest / epoch → sandbox / pod → workflow / run → operation → redacted decision / receipt.",
        )
        return figure(
            key,
            "10 / Authority changes at each runtime hop",
            body,
            702,
            "The dashed bridge denotes a proposed integration seam. Current qualification covers separate OpenShell and kernel fixtures, plus a real gateway / Temporal / collector chain; the combined production topology is not yet qualified.",
        )
    if key == "integration-profiling-loop":
        body = label(
            24, 28, "OBSERVED BEHAVIOR IS EVIDENCE, NEVER SELF-APPROVING POLICY", 12, weight=700
        )
        stages = [
            (
                24,
                62,
                "Capture scoped workload evidence",
                [
                    "Release / pod / cgroup attribution",
                    "Controlled evals + warm-up coverage",
                    "Record event loss and unknowns",
                ],
                "blue",
            ),
            (
                382,
                62,
                "Partition event outcomes",
                [
                    "Allowed → candidate requirement",
                    "Denied → negative-test evidence",
                    "Unknown → review, no silent allow",
                ],
                "orange",
            ),
            (
                740,
                62,
                "Explain proposed capability delta",
                [
                    "Normalize paths, peers and commands",
                    "Compare baseline and declared intent",
                    "Missing observations ≠ unnecessary",
                ],
                "teal",
            ),
            (
                740,
                256,
                "Approve bounded policy",
                [
                    "Intersect with organization limits",
                    "Owner reviews new authority",
                    "No automatic broadening",
                ],
                "orange",
            ),
            (
                382,
                256,
                "Translate and verify adapters",
                [
                    "Reject unsupported semantics",
                    "Pin native APIs / enforcement hooks",
                    "Check effective policy receipts",
                ],
                "blue",
            ),
            (
                24,
                256,
                "Qualify positive + negative paths",
                [
                    "Expected activity succeeds",
                    "Bypass and forbidden action fail",
                    "Changed behavior restarts the loop",
                ],
                "teal",
            ),
        ]
        for x, y, title, lines, tone in stages:
            body += box(x, y, title, lines, tone)
        body += edge([(340, 118), (376, 118)]) + edge([(698, 118), (734, 118)])
        body += edge([(898, 174), (898, 250)])
        body += edge([(740, 312), (704, 312)]) + edge([(382, 312), (346, 312)])
        body += edge([(182, 256), (182, 180)], True)
        body += band(
            432,
            "Profile provenance and limits",
            "Bind candidates to source, test scenario, platform versions and evidence hashes. CLI candidate generation is implemented; production adapters are targets.",
        )
        return figure(
            key,
            "11 / The profiling loop contains explicit approval gates",
            body,
            518,
            "The original automation proposal becomes a governed evidence loop. A denied action never enters the candidate allow set; a quiet profile never proves that an untested path is safe.",
        )
    if key == "case-package-to-release":
        body = label(
            24,
            28,
            "INCIDENT TRIAGE • SAME PORTABLE PACKAGE, DIFFERENT RELEASE GATES",
            12,
            weight=700,
        )
        body += box(
            24,
            60,
            "Developer-owned package",
            [
                "agent.yaml + typed factory",
                "Runbook / status / ticket contracts",
                "Versioned deterministic eval scenarios",
            ],
            "teal",
            height=112,
        )
        body += box(
            382,
            60,
            "Local reference demonstration",
            [
                "Synthetic status, approved runbook",
                "Exact ticket grant → local SQLite",
                "Retry returns the same receipt",
            ],
            "blue",
        )
        body += box(
            740,
            60,
            "CI evidence candidate",
            [
                "Strict validation + eval provenance",
                "T2, supervised, internal data",
                "Unsigned catalog + replicas: 0",
            ],
            "blue",
        )
        body += edge([(340, 116), (376, 116)]) + edge([(698, 116), (734, 116)])
        body += box(
            24,
            278,
            "Sandbox / routing qualification",
            [
                "OpenShell effective policy receipt",
                "Gateway / CNI bypass denials",
                "Tetragon evidence + alert checks",
            ],
            "teal",
        )
        body += box(
            382,
            278,
            "Effect and identity qualification",
            [
                "Trusted reviewer + signed grant",
                "Resource-scoped receiver auth",
                "Durable budget + fencing tests",
            ],
            "orange",
        )
        body += box(
            740,
            278,
            "Release decision",
            [
                "Evidence + approval + signed digest",
                "Gate readiness on control receipts",
                "Activate with revocation drill",
            ],
            "orange",
        )
        body += edge([(898, 172), (898, 240), (182, 240), (182, 272)], True)
        body += edge([(340, 334), (376, 334)]) + edge([(698, 334), (734, 334)])
        body += label(
            296,
            217,
            "Production work begins here: the demo is not deployment authorization",
            12,
            color="#bd5e30",
        )
        body += band(
            444,
            "What the developer sees",
            "just incident-triage → inspect proposal, approval and receipt; just examples → reproduce gates; render → withheld Kubernetes candidate.",
        )
        return figure(
            key,
            "12 / Follow the incident package through its release gates",
            body,
            530,
            "The local example exercises concrete contracts. Dashed transition: additional production services and qualification are required before any real ticket or deployment can be authorized.",
        )
    if key == "case-execution-sequence":
        names = [
            "Intake / workflow",
            "Sandboxed reader",
            "Gateway / status",
            "Trusted reviewer",
            "Ticket receiver",
        ]
        xs = [120, 330, 540, 750, 960]
        body = label(
            24,
            26,
            "TARGET PRODUCTION SEQUENCE • LOCAL DEMO EXERCISES PROPOSAL / GRANT / RECEIPT CONTRACTS",
            12,
            weight=700,
        )
        for x, name in zip(xs, names, strict=True):
            body += box(x - 96, 52, name, [], "ink", width=192, height=48)
            body += f'<path d="M{x},110 L{x},790" stroke="#a5bec5" stroke-dasharray="4 5"/>'
        messages = [
            (156, 0, 1, "1  Start bounded read phase", False, "teal"),
            (220, 1, 2, "2  JWT + read_status({})", False, "teal"),
            (284, 2, 1, "3  Resource-authorized status", True, "blue"),
            (348, 1, 0, "4  Exact proposal + payload digest", True, "blue"),
            (432, 0, 3, "5  Present immutable ticket for review", False, "orange"),
            (500, 3, 0, "6  Authenticated grant + expiry", True, "orange"),
            (
                588,
                0,
                4,
                "7  Separate effect Activity: grant + stable operation key",
                False,
                "orange",
            ),
            (654, 4, 0, "8  Revalidate rights / epoch → atomic effect + receipt", True, "teal"),
            (730, 0, 4, "9  Retry same key + same payload → same receipt", False, "blue"),
        ]
        for y, left, right, text, dashed, tone in messages:
            start, end = xs[left], xs[right]
            body += edge([(start, y), (end, y)], dashed, tone)
            body += label(min(start, end) + 9, y - 12, text, 12, PALETTE[tone][1], 600)
        body += label(243, 188, "Approved runbook is mounted / read via a narrow capability", 11)
        body += band(
            818,
            "Durability and effect semantics",
            "Temporal retries are at least once. The receiver enforces idempotency; an approval Signal alone cannot authenticate the reviewer.",
            "orange",
        )
        body += band(
            904,
            "Implementation boundary",
            "Current read-only Temporal worker rejects ticket.write. The full two-phase sandbox bridge and production ticket receiver remain proposed.",
        )
        return figure(
            key,
            "13 / One incident, one approved payload, one ticket effect",
            body,
            992,
            "Solid arrows carry requests; dashed arrows return results. Read and write authority are separated in the target design. The local fixture uses explicit runtime dependencies and an atomic SQLite receiver, with no external ticket creation.",
        )
    if key == "case-revocation":
        body = label(
            24,
            28,
            "STOP IS A DISTRIBUTED STATE TRANSITION, NOT A SINGLE PROCESS FLAG",
            12,
            weight=700,
        )
        body += band(
            60,
            "Incident response decision: revoke release / capability at policy epoch n + 1",
            "Publish a scoped reason and audit identity; freeze new grants and new work before collecting acknowledgements.",
            "orange",
            84,
        )
        targets = [
            (
                24,
                210,
                "Workflow / intake",
                [
                    "Reject new runs and approvals",
                    "Cancel workflow + outstanding tasks",
                    "Bound non-cooperative activities",
                ],
                "blue",
            ),
            (
                382,
                210,
                "Gateway / identity",
                [
                    "Deny routes at current epoch",
                    "Revoke or bound token lifetime",
                    "Account for cache and clock leeway",
                ],
                "teal",
            ),
            (
                740,
                210,
                "Receiver / sandbox",
                [
                    "Fence uncommitted effects",
                    "Terminate sandbox; verify cessation",
                    "Keep committed receipts intact",
                ],
                "orange",
            ),
        ]
        for x, y, title, lines, tone in targets:
            body += box(x, y, title, lines, tone)
            body += edge([(x + 158, 144), (x + 158, 204)], True)
            body += edge([(x + 158, 322), (x + 158, 392)], True)
        body += band(
            398,
            "Collect effective-state acknowledgements",
            "Measure each boundary's epoch, timestamp and enforcement result; missing acknowledgements keep the release blocked.",
            "blue",
            84,
        )
        body += band(
            520,
            "Recovery gate",
            "Reconcile tickets already committed; compensate via a separately approved operation. Requalify and approve a new epoch before restart.",
            "teal",
            84,
        )
        body += edge([(540, 482), (540, 514)])
        body += label(
            24,
            646,
            "Local example: cooperative STOP and SQLite receipt checks only. Distributed fencing and revocation are production targets.",
            12,
            color="#bd5e30",
        )
        return figure(
            key,
            "14 / Revoke authority at every consequential boundary",
            body,
            678,
            "Parallel dashed arrows represent independent propagation and acknowledgement. Process termination cannot undo a committed ticket; reliable revocation requires receiver-side fencing and measured enforcement lag.",
        )
    raise ValueError(f"Unknown technical diagram: {key}")
