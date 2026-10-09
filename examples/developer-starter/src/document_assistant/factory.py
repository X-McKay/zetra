"""Validate construction, then mediate only the declared read dependency."""

from dataclasses import dataclass
from pathlib import Path

from .config import load_config
from .dependencies import Dependencies
from .fixture import OfflineAnswerFixture
from .models import Answer, ContractError, Request
from .tools import ApprovedFileReader


@dataclass
class DocumentAssistant:
    dependencies: Dependencies

    def run(self, value: object) -> dict:
        request = Request.parse(value)
        if len(request.document_ids) > self.dependencies.max_documents:
            raise ContractError("Request exceeds the configured document limit")
        documents = tuple(
            self.dependencies.documents.read(identifier) for identifier in request.document_ids
        )
        result = self.dependencies.answerer.answer(
            request.question, documents, self.dependencies.instructions
        )
        # Check the wire contract again at the adapter boundary and reject
        # citations to material that was not supplied for this request.
        answer = Answer.parse(result.wire())
        supplied = {document.identifier for document in documents}
        if set(answer.source_ids) - supplied:
            raise ContractError("Answer cites evidence that was not supplied")
        return answer.wire()


def create_application(root: Path) -> DocumentAssistant:
    root = root.resolve()
    config = load_config(root)
    try:
        instructions = config.instruction_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ContractError("Instruction file could not be read") from exc
    if not instructions.strip() or len(instructions) > 10000:
        raise ContractError("Instructions must be nonempty text <= 10000 characters")
    dependencies = Dependencies(
        ApprovedFileReader(config.document_root, config.allowed_document_ids),
        OfflineAnswerFixture(),
        instructions,
        config.max_documents,
    )
    return DocumentAssistant(dependencies)
