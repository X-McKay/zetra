from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from agentctl.cli import main
from agentctl.risk import load_schema, matrix_tier, schema_path


class RiskContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
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
        self.risk_path = self.root / "docs/risk-assessments/support-agent.yaml"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _load(self) -> dict:
        return yaml.safe_load(self.risk_path.read_text(encoding="utf-8"))

    def _write(self, payload: dict) -> None:
        self.risk_path.write_text(
            yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
        )

    def _json_command(self, arguments: list[str]) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            result = main([*arguments, "--format", "json"])
        return result, json.loads(output.getvalue())

    def test_schema_is_packaged_and_valid_draft_2020_12(self) -> None:
        self.assertTrue(schema_path().is_file())
        schema = load_schema()
        self.assertEqual(
            schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
        )

    def test_matrix_matches_published_contract(self) -> None:
        self.assertEqual(matrix_tier(1, 1), "low")
        self.assertEqual(matrix_tier(2, 4), "high")
        self.assertEqual(matrix_tier(3, 4), "critical")
        self.assertEqual(matrix_tier(4, 1), "high")

    def test_validate_discovers_generated_assessment(self) -> None:
        scenario = self._load()["scenarios"][0]
        self.assertEqual(scenario["status"], "open")
        self.assertTrue(scenario["affected_assets"])
        self.assertTrue(scenario["preconditions"])
        self.assertTrue(scenario["consequences"])

        result, payload = self._json_command(
            ["risk", "validate", "--root", str(self.root)]
        )
        self.assertEqual(result, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            {item["code"] for item in payload["diagnostics"]},
            {"RISK101", "RISK102"},
        )

    def test_explain_emits_stable_json_envelope(self) -> None:
        result, payload = self._json_command(
            [
                "risk",
                "explain",
                self.risk_path.relative_to(self.root).as_posix(),
                "--root",
                str(self.root),
            ]
        )
        self.assertEqual(result, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["path"], "docs/risk-assessments/support-agent.yaml")
        self.assertEqual(payload["assessment"]["assessment"]["agent"], "support-agent")

    def test_schema_failure_has_json_path(self) -> None:
        payload = self._load()
        del payload["scope"]["intended_use"]
        self._write(payload)
        result, report = self._json_command(
            ["risk", "validate", str(self.risk_path), "--root", str(self.root)]
        )
        self.assertEqual(result, 1)
        self.assertFalse(report["ok"])
        diagnostic = report["diagnostics"][0]
        self.assertEqual(diagnostic["code"], "RISK003")
        self.assertIn("$.scope", diagnostic["message"])

    def test_semantic_failure_detects_matrix_mismatch(self) -> None:
        payload = self._load()
        payload["scenarios"][0]["residual"]["tier"] = "low"
        self._write(payload)
        result, report = self._json_command(
            ["risk", "validate", str(self.risk_path), "--root", str(self.root)]
        )
        self.assertEqual(result, 1)
        self.assertIn("RISK005", {item["code"] for item in report["diagnostics"]})

    def test_semantic_failure_detects_prohibited_use_decision(self) -> None:
        payload = self._load()
        payload["regulatory_screen"]["prohibited_use"] = True
        self._write(payload)
        result, report = self._json_command(
            ["risk", "validate", str(self.risk_path), "--root", str(self.root)]
        )
        self.assertEqual(result, 1)
        self.assertIn("RISK009", {item["code"] for item in report["diagnostics"]})

    def test_project_validation_checks_cross_artifact_consistency(self) -> None:
        spec_path = self.root / "src/sample_agents/agents/support_agent/agent.yaml"
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        spec["description"] = "Investigate support requests and propose resolutions."
        spec["metadata"]["risk_tier"] = "high"
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

        result, report = self._json_command(["validate", "--root", str(self.root)])
        self.assertEqual(result, 1)
        self.assertIn("AGENT040", {item["code"] for item in report["diagnostics"]})


if __name__ == "__main__":
    unittest.main()
