"""Run the offline example. No model, network or application data writes."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from document_assistant.factory import create_application  # noqa: E402
from document_assistant.models import ContractError  # noqa: E402
from document_assistant.tools import DocumentDenied, DocumentUnavailable  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True)
    parser.add_argument("--document", action="append", required=True)
    args = parser.parse_args()
    try:
        answer = create_application(ROOT).run(
            {"question": args.question, "document_ids": args.document}
        )
    except (ContractError, DocumentDenied, DocumentUnavailable) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
        return 1
    print(json.dumps(answer, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
