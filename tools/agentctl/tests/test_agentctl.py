from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from agentctl.cli import main


class AgentctlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_scaffold_and_validate_durable_agent(self) -> None:
        result = main(
            [
                "scaffold",
                "support-agent",
                "--root",
                str(self.root),
                "--package",
                "sample_agents",
                "--owner",
                "support-platform",
                "--execution-class",
                "durable",
                "--risk-tier",
                "medium",
            ]
        )
        self.assertEqual(result, 0)
        spec_path = (
            self.root
            / "src"
            / "sample_agents"
            / "agents"
            / "support_agent"
            / "agent.yaml"
        )
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        spec["description"] = "Investigate support requests and propose resolutions."
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
        self.assertEqual(main(["validate", "--root", str(self.root)]), 0)
        self.assertTrue(
            (self.root / "src/sample_agents/workflows/support_agent.py").is_file()
        )
        self.assertTrue(
            (self.root / "src/sample_agents/activities/support_agent.py").is_file()
        )

    def test_scaffold_refuses_overwrite(self) -> None:
        arguments = [
            "scaffold",
            "support-agent",
            "--root",
            str(self.root),
            "--owner",
            "support-platform",
        ]
        self.assertEqual(main(arguments), 0)
        self.assertEqual(main(arguments), 2)

    def test_scaffold_adds_second_agent_without_overwriting_package_files(self) -> None:
        package_init = self.root / "src/acme_agents/__init__.py"
        package_init.parent.mkdir(parents=True)
        package_init.write_text('PACKAGE_NAME = "existing"\n', encoding="utf-8")
        for name in ("first-agent", "second-agent"):
            self.assertEqual(
                main(
                    [
                        "scaffold",
                        name,
                        "--root",
                        str(self.root),
                        "--owner",
                        "agent-platform",
                    ]
                ),
                0,
            )
        self.assertEqual(
            package_init.read_text(encoding="utf-8"), 'PACKAGE_NAME = "existing"\n'
        )
        self.assertTrue(
            (self.root / "src/acme_agents/agents/second_agent/agent.yaml").is_file()
        )

    def test_validate_rejects_ephemeral_write(self) -> None:
        self.assertEqual(
            main(
                [
                    "scaffold",
                    "writer-agent",
                    "--root",
                    str(self.root),
                    "--owner",
                    "operations",
                ]
            ),
            0,
        )
        spec_path = self.root / "src/acme_agents/agents/writer_agent/agent.yaml"
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        spec["description"] = "Update a controlled operational record."
        spec["metadata"]["enabled_toolsets"] = ["records"]
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
        tool_dir = self.root / "src/acme_agents/tools/records"
        tool_dir.mkdir(parents=True)
        (tool_dir / "tool.yaml").write_text(
            """name: update-record
owner: operations
effect: write_reversible
retry_safety: idempotency_key_required
authorization_scopes: [records.write]
timeout_seconds: 10
data_classification: internal
max_output_bytes: 4096
""",
            encoding="utf-8",
        )
        self.assertEqual(main(["validate", "--root", str(self.root)]), 1)

    def test_skills_validate(self) -> None:
        skill = self.root / "skills" / "review-agent"
        references = skill / "references"
        references.mkdir(parents=True)
        (references / "standard.md").write_text("# Standard\n", encoding="utf-8")
        (skill / "SKILL.md").write_text(
            """---
name: review-agent
description: Review an agent contract. Use when preparing an agent release.
---

# Review Agent

Read [the standard](references/standard.md), then report findings.
""",
            encoding="utf-8",
        )
        self.assertEqual(main(["skills", "validate", "--root", str(self.root)]), 0)
        (references / "standard.md").unlink()
        self.assertEqual(main(["skills", "validate", "--root", str(self.root)]), 1)

    def test_eval_run_adds_provenance(self) -> None:
        adapter = self.root / "adapter.py"
        adapter.write_text(
            """import json, os
from pathlib import Path
Path(os.environ['AGENTCTL_REPORT_PATH']).write_text(json.dumps({
    'schema_version': 1,
    'agent': os.environ['AGENTCTL_AGENT'],
    'hard_gates': {'safe': True},
    'metrics': {'task_success_rate': 0.95},
}), encoding='utf-8')
""",
            encoding="utf-8",
        )
        report = self.root / "report.json"
        result = main(
            [
                "eval",
                "run",
                "support-agent",
                "--root",
                str(self.root),
                "--report",
                str(report),
                "--command",
                sys.executable,
                str(adapter),
            ]
        )
        self.assertEqual(result, 0)
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertIn("agentctl_completed_at", payload["provenance"])
        self.assertIn("eval_command", payload["provenance"])

    def test_release_check_passes_and_detects_regression(self) -> None:
        report = self.root / "report.json"
        baseline = self.root / "baseline.json"
        policy = self.root / "policy.yaml"
        report.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "agent": "support-agent",
                    "hard_gates": {"safe": True},
                    "metrics": {"task_success_rate": 0.94, "cost": 0.08},
                }
            ),
            encoding="utf-8",
        )
        baseline.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "agent": "support-agent",
                    "hard_gates": {"safe": True},
                    "metrics": {"task_success_rate": 0.95, "cost": 0.07},
                }
            ),
            encoding="utf-8",
        )
        policy.write_text(
            """schema_version: 1
hard_gates:
  safe: true
thresholds:
  task_success_rate: {min: 0.90}
  cost: {max: 0.10}
regressions:
  task_success_rate: {direction: higher, max_delta: 0.02}
  cost: {direction: lower, max_delta: 0.02}
""",
            encoding="utf-8",
        )
        arguments = [
            "release",
            "check",
            "--report",
            str(report),
            "--policy",
            str(policy),
            "--baseline",
            str(baseline),
        ]
        self.assertEqual(main(arguments), 0)
        payload = json.loads(report.read_text(encoding="utf-8"))
        payload["metrics"]["task_success_rate"] = 0.80
        report.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(main(arguments), 1)

    def test_release_check_requires_baseline_for_regression_policy(self) -> None:
        report = self.root / "report.json"
        policy = self.root / "policy.yaml"
        report.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "agent": "support-agent",
                    "hard_gates": {"safe": True},
                    "metrics": {"task_success_rate": 0.95},
                }
            ),
            encoding="utf-8",
        )
        policy.write_text(
            """schema_version: 1
hard_gates: {safe: true}
regressions:
  task_success_rate: {direction: higher, max_delta: 0.02}
""",
            encoding="utf-8",
        )
        self.assertEqual(
            main(
                [
                    "release",
                    "check",
                    "--report",
                    str(report),
                    "--policy",
                    str(policy),
                ]
            ),
            1,
        )

    def test_release_check_supports_system_subjects(self) -> None:
        report = self.root / "report.json"
        baseline = self.root / "baseline.json"
        policy = self.root / "policy.yaml"
        payload = {
            "schema_version": 1,
            "subject": {"kind": "system", "name": "research-system"},
            "hard_gates": {"safe": True},
            "metrics": {"task_success_rate": 0.95},
        }
        report.write_text(json.dumps(payload), encoding="utf-8")
        baseline.write_text(json.dumps(payload), encoding="utf-8")
        policy.write_text(
            "schema_version: 1\nhard_gates: {safe: true}\n",
            encoding="utf-8",
        )
        arguments = [
            "release",
            "check",
            "--report",
            str(report),
            "--policy",
            str(policy),
            "--baseline",
            str(baseline),
        ]
        self.assertEqual(main(arguments), 0)
        mismatched = dict(payload)
        mismatched["subject"] = {"kind": "agent", "name": "research-system"}
        baseline.write_text(json.dumps(mismatched), encoding="utf-8")
        self.assertEqual(main(arguments), 1)

    def test_force_color_overrides_no_color_environment_for_help(self) -> None:
        environment = dict(os.environ)
        environment["NO_COLOR"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "agentctl", "--force-color", "--help"],
            env=environment,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"\x1b[", result.stdout)

    def test_force_color_environment_styles_help(self) -> None:
        environment = dict(os.environ)
        environment.pop("NO_COLOR", None)
        environment["FORCE_COLOR"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "agentctl", "--help"],
            env=environment,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"\x1b[", result.stdout)

    def test_json_output_never_contains_terminal_styling(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agentctl",
                "--force-color",
                "validate",
                "--root",
                str(self.root),
                "--format",
                "json",
            ],
            capture_output=True,
            check=False,
        )
        self.assertNotIn(b"\x1b[", result.stdout)
        json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
