"""Small plain-language diagrams for the developer's start-to-finish guide."""

from technical_diagrams import band, box, edge, figure, label


def lifecycle_diagram(key):
    if key == "dlc-overview":
        body = label(
            24, 28, "ONE TASK → WORKING CODE → CHECKED RELEASE → DAILY OPERATION", 12, weight=700
        )
        stages = [
            (
                24,
                58,
                "1 / Decide the task",
                ["Write the goal and limits", "Name the owner"],
                "teal",
            ),
            (
                382,
                58,
                "2 / Create the files",
                ["Manifest, typed code and tools", "Keep secrets outside the package"],
                "blue",
            ),
            (
                740,
                58,
                "3 / Make one run work",
                ["Use fake tools first", "Inspect the exact result"],
                "teal",
            ),
            (
                740,
                228,
                "4 / Test expected + bad inputs",
                ["Useful answers and denied actions", "Save results tied to this code"],
                "orange",
            ),
            (
                382,
                228,
                "5 / Reduce wasted work",
                ["Measure quality, time and cost", "Change one thing; test again"],
                "blue",
            ),
            (
                24,
                228,
                "6 / Review the change",
                ["CI runs checks automatically", "Prepare a plan for review"],
                "orange",
            ),
        ]
        for x, y, title, lines, tone in stages:
            body += box(x, y, title, lines, tone, height=98)
        body += edge([(340, 108), (376, 108)]) + edge([(698, 108), (734, 108)])
        body += edge([(898, 156), (898, 222)])
        body += edge([(740, 276), (704, 276)]) + edge([(382, 276), (346, 276)])
        body += band(
            374,
            "7 / Approve and deploy → 8 / Monitor, fix and update",
            "Check the real deployment first. Watch results, turn failures into tests, and review each change before releasing it.",
            "teal",
            78,
        )
        body += edge([(182, 326), (182, 368)])
        return figure(
            key,
            "The eight steps",
            body,
            474,
            "Work down the guide in order. After release, monitoring feeds the next tested change.",
        )
    if key == "dlc-package-map":
        body = label(24, 28, "FILES HAVE DIFFERENT JOBS", 12, weight=700)
        body += box(
            24,
            58,
            "Describe the task",
            [
                "agent.yaml: owner, access, limits",
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
                "tools/: small, checked operations",
            ],
            "blue",
        )
        body += box(
            740,
            58,
            "Check the task",
            [
                "tests/: expected + forbidden behavior",
                "evals/scenarios.json: runnable cases",
                "datasets + baselines: quality checks",
            ],
            "orange",
        )
        body += edge([(340, 114), (376, 114)]) + edge([(698, 114), (734, 114)])
        body += band(
            220,
            "Project-wide files",
            "pyproject.toml + uv.lock: dependencies; policy/: review notes; docs/: operating instructions; deploy/: generated plans.",
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
            "The root manifest is what the current CLI reads. Optional folders organize a larger agent; empty folders do not satisfy any check.",
        )
    if key == "dlc-test-loop":
        body = label(24, 28, "TEST WHAT SHOULD HAPPEN AND WHAT MUST NEVER HAPPEN", 12, weight=700)
        body += box(
            24,
            58,
            "Choose concrete cases",
            [
                "Expected input and result",
                "Bad input, denied access, retry",
                "Case IDs stay the same over time",
            ],
            "teal",
        )
        body += box(
            382,
            58,
            "Run and inspect",
            [
                "Unit tests: exact assertions",
                "Model checks: repeated sample runs",
                "Platform checks: real boundaries",
            ],
            "blue",
        )
        body += box(
            740,
            58,
            "Keep or fix the change",
            [
                "All required safety cases pass",
                "Quality meets the agreed target",
                "Results match this code version",
            ],
            "orange",
        )
        body += edge([(340, 114), (376, 114)]) + edge([(698, 114), (734, 114)])
        body += edge([(898, 170), (898, 216), (182, 216), (182, 176)], True, "orange")
        body += label(316, 205, "Failure → fix code or coverage → run again", 12, color="#bd5e30")
        body += band(
            264,
            "Choose tests that match the agent's actions",
            "All agents: inputs, outputs, limits and failures. Add approval and retry tests for writes; access checks for connected tools.",
            "teal",
            78,
        )
        return figure(
            key,
            "Check useful behavior and the limits on actions",
            body,
            364,
            "The offline examples check program behavior. They do not measure a real model's answer quality or prove production isolation.",
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
            "Model-dependent changes need real model evaluation. The current local budget helper is a work limit, not a verified billing ledger.",
        )
    if key == "dlc-release":
        body = label(24, 28, "AUTOMATED CHECKS AND DEPLOYMENT ARE SEPARATE STEPS", 12, weight=700)
        body += box(
            24,
            58,
            "Developer / reviewer",
            ["Open a pull request", "Review code, tools and test cases", "Run just check locally"],
            "teal",
        )
        body += box(
            382,
            58,
            "CI: automatic repository checks",
            [
                "Locked tools, lint, types and tests",
                "Example evaluations + HTML build",
                "Passing CI is not release approval",
            ],
            "blue",
        )
        body += box(
            740,
            58,
            "Current local output",
            [
                "Evaluation result + catalog draft",
                "Image-pinned Kubernetes plan",
                "replicas: 0 — no workload starts",
            ],
            "blue",
        )
        body += edge([(340, 114), (376, 114)]) + edge([(698, 114), (734, 114)])
        body += box(
            24,
            278,
            "Platform checks",
            [
                "Build and verify the exact image",
                "Install approved access controls",
                "Test allowed and forbidden paths",
            ],
            "orange",
        )
        body += box(
            382,
            278,
            "Owner approves release",
            [
                "Review results and open risks",
                "Check stop and recovery procedure",
                "Authorize this version / environment",
            ],
            "orange",
        )
        body += box(
            740,
            278,
            "CD: controlled deployment",
            [
                "Start a small supervised release",
                "Watch results and error rate",
                "Pause if agreed limits are exceeded",
            ],
            "teal",
        )
        body += edge([(898, 170), (898, 226), (182, 226), (182, 272)], True)
        body += label(
            286,
            214,
            "Production deployment service still needs to be provided",
            12,
            color="#bd5e30",
        )
        body += edge([(340, 334), (376, 334)]) + edge([(698, 334), (734, 334)])
        return figure(
            key,
            "From pull request to an approved deployment",
            body,
            414,
            "The top row is supported by this repository. The lower row is the required production process, not an automatic Zetra deployment feature.",
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
