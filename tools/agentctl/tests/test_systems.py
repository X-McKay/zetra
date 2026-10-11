from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from agentctl.cli import main
from agentctl.systems import load_system_schema


class SystemContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self._scaffold_agent("planner-agent", "planner")
        self._scaffold_agent("worker-agent", "worker")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _scaffold_agent(self, name: str, purpose: str) -> None:
        result = main(
            [
                "scaffold",
                name,
                "--root",
                str(self.root),
                "--package",
                "sample_agents",
                "--owner",
                "agent-platform",
                "--execution-class",
                "durable",
                "--risk-tier",
                "medium",
            ]
        )
        self.assertEqual(result, 0)
        path = (
            self.root
            / "src"
            / "sample_agents"
            / "agents"
            / name.replace("-", "_")
            / "agent.yaml"
        )
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        payload["description"] = f"Perform the bounded {purpose} role."
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    def _scaffold_system(self, *extra: str) -> Path:
        result = main(
            [
                "system",
                "scaffold",
                "research-system",
                "--root",
                str(self.root),
                "--package",
                "sample_agents",
                "--owner",
                "ai-platform",
                "--operational-owner",
                "research-operations",
                "--execution-class",
                "durable",
                "--risk-tier",
                "medium",
                "--member",
                "planner-agent:coordinator",
                "--member",
                "worker-agent:worker",
                *extra,
            ]
        )
        self.assertEqual(result, 0)
        path = self.root / "src/sample_agents/systems/research_system/system.yaml"
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        payload["description"] = "Coordinate bounded planning and research work."
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return path

    def test_schema_is_packaged_and_valid(self) -> None:
        Draft202012Validator.check_schema(load_system_schema())

    def test_scaffold_validate_graph_and_eval(self) -> None:
        path = self._scaffold_system()
        self.assertEqual(main(["system", "validate", "--root", str(self.root)]), 0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(
                [
                    "system",
                    "graph",
                    str(path),
                    "--root",
                    str(self.root),
                    "--format",
                    "json",
                ]
            )
        self.assertEqual(result, 0)
        graph = json.loads(output.getvalue())
        self.assertEqual(graph["system"], "research-system")
        self.assertEqual(len(graph["members"]), 2)

        report = self.root / "artifacts/evals/research-system.json"
        self.assertEqual(
            main(
                [
                    "eval",
                    "run",
                    "research-system",
                    "--system",
                    "--root",
                    str(self.root),
                ]
            ),
            0,
        )
        payload = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["subject"], {"kind": "system", "name": "research-system"}
        )
        self.assertIn("git_commit", payload["provenance"])

    def test_validation_rejects_member_version_drift(self) -> None:
        path = self._scaffold_system()
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        payload["members"][0]["version"] = "9.9.9"
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        self.assertEqual(main(["system", "validate", "--root", str(self.root)]), 1)

    def test_dynamic_membership_scaffolds_admission_contracts(self) -> None:
        path = self._scaffold_system(
            "--dynamic-membership", "--topology", "dynamic_membership"
        )
        self.assertTrue((path.parent / "policies/registry.yaml").is_file())
        self.assertTrue((path.parent / "policies/admission.yaml").is_file())
        self.assertEqual(main(["system", "validate", "--root", str(self.root)]), 0)


if __name__ == "__main__":
    unittest.main()
