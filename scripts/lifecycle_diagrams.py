"""Small plain-language diagrams for the developer's start-to-finish guide."""

from technical_diagrams import band, box, edge, figure, label


def three_steps(key, title, stages, summary, caption):
    body = label(24, 28, title.upper(), 12, weight=700)
    for x, (heading, lines, tone) in zip((24, 382, 740), stages, strict=True):
        body += box(x, 58, heading, lines, tone)
    body += edge([(340, 114), (376, 114)]) + edge([(698, 114), (734, 114)])
    body += band(222, summary[0], summary[1], "ink", 78)
    return figure(key, title, body, 326, caption)


def lifecycle_diagram(key):
    extra = {
        "dlc-skills-tools": (
            "Skills guide the work; tools perform operations",
            [
                (
                    "Skill: how to do a task",
                    [
                        "Reusable instructions and examples",
                        "Load when relevant",
                        "May explain when to use a tool",
                    ],
                    "teal",
                ),
                (
                    "Tool: a callable operation",
                    [
                        "A name and typed arguments",
                        "Validate inputs, limits and access",
                        "Return structured results",
                    ],
                    "blue",
                ),
                (
                    "Service: enforce permission",
                    [
                        "Authenticate the actual caller",
                        "Check resource and action rights",
                        "Require approval for risky writes",
                    ],
                    "orange",
                ),
            ],
            (
                "Example: answer from approved documents",
                "Skill: check sources and cite evidence. Tool: read_document(id). Service: restrict which documents this user can read.",
            ),
            "Skill instructions cannot grant credentials or override permission checks. Treat document and tool contents as untrusted data.",
        ),
        "dlc-mcp": (
            "MCP standardizes the connection",
            [
                (
                    "Your application",
                    [
                        "Host owns the agent and user UI",
                        "MCP client connects to a server",
                        "Select only needed capabilities",
                    ],
                    "teal",
                ),
                (
                    "MCP server",
                    [
                        "Advertises tools and schemas",
                        "May offer resources and prompts",
                        "Validate each incoming request",
                    ],
                    "blue",
                ),
                (
                    "Connected service",
                    [
                        "Documents, search or another API",
                        "Enforce user and resource access",
                        "Return permitted information",
                    ],
                    "orange",
                ),
            ],
            (
                "Connect only what the task needs",
                "Choose the server, enable only needed tools, and verify user access. Check failures as well as successful requests.",
            ),
            "Requests go left to right; results return through the connection. Retrieved text must not grant new authority.",
        ),
        "dlc-evaluations": (
            "Evaluations measure how well the task is done",
            [
                (
                    "Choose cases",
                    [
                        "Common, difficult and missing data",
                        "Safety and denied-action cases",
                        "Keep a held-out comparison set",
                    ],
                    "teal",
                ),
                (
                    "Run and score",
                    [
                        "Freeze data, model and settings",
                        "Check sources and actual actions",
                        "Repeat variable model trials",
                    ],
                    "blue",
                ),
                (
                    "Inspect results",
                    [
                        "Compare each task and category",
                        "Review failures and judge errors",
                        "Save results with release details",
                    ],
                    "orange",
                ),
            ],
            (
                "Choose scoring that fits the output",
                "Check output fields and actual actions with code. Review answer quality against evidence and a clear scoring guide.",
            ),
            "An average can hide failures in an important category. Inspect those categories and critical cases separately.",
        ),
        "dlc-release": (
            "Release only when all required gates pass",
            [
                (
                    "CI: verify the change",
                    [
                        "Run the automated tests",
                        "Run versioned evaluations",
                        "Record code, model and settings",
                    ],
                    "blue",
                ),
                (
                    "Review both kinds of gate",
                    [
                        "Absolute: meet a fixed target",
                        "Relative: compare with baseline",
                        "Critical safety cases must pass",
                    ],
                    "orange",
                ),
                (
                    "CD: deploy and observe",
                    [
                        "Review access and configuration",
                        "Start small and monitor outcomes",
                        "Keep a tested rollback plan",
                    ],
                    "teal",
                ),
            ],
            (
                "Example quality gates: both must pass",
                "Candidate success ≥ 90%; candidate minus approved baseline ≥ −1 percentage point. Also check cost, latency and critical cases.",
            ),
            "Thresholds are examples. Compare the same cases under the same conditions and agree how uncertainty affects the release decision.",
        ),
    }
    if key in extra:
        return three_steps(key, *extra[key])
    if key == "dlc-overview":
        body = label(24, 28, "BUILD IN SMALL STEPS; RETURN TO THEM AS YOU LEARN", 12, weight=700)
        stages = [
            ("1 / Define the problem", ["Outcome, success measures", "Data, access and limits"]),
            (
                "2 / Set up the repository",
                ["Clear files and dependencies", "Repeatable local commands"],
            ),
            (
                "3 / Add skills and tools",
                ["Instructions guide behavior", "Code validates operations"],
            ),
            (
                "4 / Connect MCP servers",
                ["A standard connection protocol", "Review server access and trust"],
            ),
            (
                "5 / Test the software",
                ["Inputs, connections and failures", "Check forbidden behavior"],
            ),
            (
                "6 / Evaluate the behavior",
                ["Representative tasks and scoring", "Measure usefulness and safety"],
            ),
            ("7 / Optimize", ["Compare quality, cost and time", "Keep changes that meet targets"]),
            ("8 / Release", ["Version relevant changes", "Review gates, deploy gradually"]),
            (
                "9 / Monitor and improve",
                ["Observe outcomes and failures", "Turn problems into new cases"],
            ),
        ]
        for i, (heading, lines) in enumerate(stages):
            row, col = divmod(i, 3)
            body += box(
                24 + col * 358,
                58 + row * 160,
                heading,
                lines,
                ("teal", "blue", "orange")[col],
                height=98,
            )
            if col < 2:
                body += edge(
                    [(340 + col * 358, 108 + row * 160), (376 + col * 358, 108 + row * 160)]
                )
            elif row < 2:
                body += edge(
                    [
                        (898, 156 + row * 160),
                        (898, 188 + row * 160),
                        (182, 188 + row * 160),
                        (182, 212 + row * 160),
                    ]
                )
        return figure(
            key,
            "The nine steps",
            body,
            498,
            "Start with one useful task. Revisit the checks whenever code, instructions, models, data or access change.",
        )
    if key == "dlc-package-map":
        body = label(24, 28, "FILES HAVE DIFFERENT JOBS", 12, weight=700)
        body += box(
            24,
            58,
            "Describe the task",
            [
                "application.toml: task and settings",
                "instructions/: purpose and stop rules",
                "models.py: allowed inputs / outputs",
            ],
            "teal",
        )
        body += box(
            382,
            58,
            "Implement the task",
            [
                "factory.py: build and run the agent",
                "dependencies.py: supplied services",
                "tools.py: small, checked operations",
            ],
            "blue",
        )
        body += box(
            740,
            58,
            "Check the task",
            [
                "tests/: expected + forbidden behavior",
                "evals/: tasks, scoring and baselines",
                "docs/: setup and recovery notes",
            ],
            "orange",
        )
        body += edge([(340, 114), (376, 114)]) + edge([(698, 114), (734, 114)])
        body += band(
            220,
            "Project-wide files",
            "pyproject.toml + uv.lock: dependencies; justfile: repeatable commands; CI: automatic checks; deploy/: deployment settings.",
            "ink",
            80,
        )
        body += band(
            328,
            "Add only when needed",
            "workflows/ + activities/: resumable jobs; runtime/: service connections; observability/: safe logs and measurements.",
            "blue",
            80,
        )
        return figure(
            key,
            "Where each kind of information belongs",
            body,
            430,
            "Suggested names help organize the work. Configuration only takes effect when application code or a deployment service reads it.",
        )
    if key == "dlc-test-loop":
        return three_steps(
            key,
            "Tests check whether the software works",
            [
                (
                    "Fast local checks",
                    [
                        "Smoke: can it start and finish?",
                        "Unit: one component in isolation",
                        "Use deterministic fake services",
                    ],
                    "teal",
                ),
                (
                    "Check the connections",
                    [
                        "Integration: components together",
                        "Contract: request / response shapes",
                        "Check errors and denied access",
                    ],
                    "blue",
                ),
                (
                    "Check a complete task",
                    [
                        "End-to-end: entry to final result",
                        "Test the actual effects and state",
                        "Include missing data and failures",
                    ],
                    "orange",
                ),
            ],
            (
                "Use checks that can catch a real defect",
                "Local fixtures keep tests repeatable. Separate live tests verify actual services, credentials, access, timeouts and limits.",
            ),
            "A passing startup check cannot establish answer quality. Evaluate that separately, and test deployed permissions on the actual service.",
        )
    if key == "dlc-cost-loop":
        body = label(24, 28, "OPTIMIZE AGAINST THE SAME CASES", 12, weight=700)
        body += box(
            24,
            58,
            "Record a starting point",
            [
                "Useful outcomes / failed outcomes",
                "Time, model calls and tool calls",
                "Actual charge and retry count",
            ],
            "blue",
        )
        body += box(
            382,
            58,
            "Try one change",
            [
                "Shorter relevant context",
                "Fewer repeated reads",
                "Smaller model, if tests support it",
            ],
            "teal",
        )
        body += box(
            740,
            58,
            "Compare the result",
            [
                "Same cases and quality target",
                "Cost per useful completed task",
                "Approval and safety checks still pass",
            ],
            "orange",
        )
        body += edge([(340, 114), (376, 114)]) + edge([(698, 114), (734, 114)])
        body += band(
            224,
            "Keep the change only when the measured trade-off is acceptable",
            "For example: 20 calls / 10 useful tasks = 2 calls per useful task. Include failed attempts, retries and review work.",
            "teal",
            78,
        )
        body += band(
            324,
            "Cache carefully",
            "Reuse approved reads within the same access scope and freshness window. Never cache an approval to authorize a different write.",
            "orange",
            78,
        )
        return figure(
            key,
            "Measure → change one thing → compare",
            body,
            424,
            "Compare on the same cases and inspect each category. Track actual usage and charges; a configured budget is not a billing measurement.",
        )
    if key == "dlc-operate":
        body = label(24, 28, "WATCH RESULTS, THEN ACT ON WHAT CHANGED", 12, weight=700)
        body += box(
            24,
            58,
            "Collect safe measurements",
            [
                "Version, run and operation IDs",
                "Success, time, cost and failures",
                "Denied actions and pending reviews",
            ],
            "blue",
        )
        body += box(
            382,
            58,
            "Inspect a problem",
            [
                "Find the run and exact version",
                "Check tool and approval decisions",
                "Check whether an effect committed",
            ],
            "teal",
        )
        body += box(
            740,
            58,
            "Contain and recover",
            [
                "Stop new work when necessary",
                "Block writes at the receiving service",
                "Preserve receipts; do not guess",
            ],
            "orange",
        )
        body += edge([(340, 114), (376, 114)]) + edge([(698, 114), (734, 114)])
        body += band(
            224,
            "Turn the problem into the next test",
            "Remove sensitive data → add a regression case → fix → evaluate → review → release → compare the monitored result.",
            "teal",
            80,
        )
        body += edge([(898, 170), (898, 218)])
        return figure(
            key,
            "Monitoring is part of the development loop",
            body,
            326,
            "A stop is not an undo. For agents that write data, also block new actions at the receiving service.",
        )
    raise ValueError(f"Unknown lifecycle diagram: {key}")
