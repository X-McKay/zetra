"""Assert exact offline fixture behavior; this is not model quality evaluation."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from document_assistant.factory import create_application  # noqa: E402
from document_assistant.models import ContractError  # noqa: E402
from document_assistant.tools import DocumentDenied, DocumentUnavailable  # noqa: E402


def evaluate(root: Path = ROOT) -> dict:
    application = create_application(root)
    dataset = (root / "evals" / "cases.jsonl").read_bytes()
    results = []
    identifiers: set[str] = set()
    for line in dataset.decode("utf-8").splitlines():
        case = json.loads(line)
        if case["id"] in identifiers:
            raise ValueError("Duplicate evaluation case ID")
        identifiers.add(case["id"])
        try:
            observed = application.run(case["request"])
        except (ContractError, DocumentDenied, DocumentUnavailable) as exc:
            observed = {"error": type(exc).__name__}
        expected = (
            {"error": case["expected_error"]} if "expected_error" in case else case["expected"]
        )
        results.append({"id": case["id"], "passed": observed == expected, "observed": observed})
    if not results:
        raise ValueError("Evaluation must contain at least one case")
    return {
        "scope": "offline fixture contracts and approved local document access",
        "answerer": "offline-extractive",
        "model_calls": 0,
        "dataset_sha256": hashlib.sha256(dataset).hexdigest(),
        "total": len(results),
        "passed": sum(1 for result in results if result["passed"]),
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate()
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
