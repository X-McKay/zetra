"""Exercise public commands, report contents and a deliberate eval failure."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class EndToEndTests(unittest.TestCase):
    def test_command_runs_from_other_working_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "run.py"),
                    "--question",
                    "What receipt is needed for expense reimbursement?",
                    "--document",
                    "expense-policy",
                ],
                cwd=folder,
                capture_output=True,
                text=True,
                check=True,
            )
        answer = json.loads(result.stdout)
        self.assertEqual(answer["source_ids"], ["expense-policy"])
        self.assertEqual(answer["outcome"], "answered")

    def test_denied_command_returns_structured_error(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run.py"),
                "--question",
                "Read budget",
                "--document",
                "private-budget",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["error"], "DocumentDenied")

    def test_evaluation_records_real_results_and_zero_model_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "evaluation.json"
            result = subprocess.run(
                [sys.executable, str(ROOT / "evaluate.py"), "--output", str(output)],
                cwd=folder,
                capture_output=True,
                text=True,
                check=True,
            )
            report = json.loads(output.read_text())
            self.assertEqual(report, json.loads(result.stdout))
        self.assertEqual(report["total"], 8)
        self.assertEqual(report["passed"], 8)
        self.assertEqual(report["model_calls"], 0)
        self.assertTrue(all(case["passed"] for case in report["results"]))

    def test_changed_expected_answer_causes_failed_evaluation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            shutil.copytree(ROOT, root, dirs_exist_ok=True)
            dataset = root / "evals/cases.jsonl"
            cases = [json.loads(line) for line in dataset.read_text().splitlines()]
            cases[0]["expected"]["text"] = "Unsupported expected answer"
            dataset.write_text(
                "\n".join(json.dumps(case) for case in cases) + "\n", encoding="utf-8"
            )
            result = subprocess.run(
                [sys.executable, str(root / "evaluate.py")],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["passed"], 7)
