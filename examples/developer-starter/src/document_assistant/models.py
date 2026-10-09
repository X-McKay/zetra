"""Small strict wire contracts. Contracts validate shape, not business truth."""

import re
from dataclasses import asdict, dataclass


class ContractError(ValueError):
    """An input, output or configuration fails the declared contract."""


def document_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", value):
        raise ContractError("Document IDs must be lowercase names, not paths")
    return value


@dataclass(frozen=True)
class Request:
    question: str
    document_ids: tuple[str, ...]

    @classmethod
    def parse(cls, value: object) -> "Request":
        if not isinstance(value, dict) or set(value) != {"question", "document_ids"}:
            raise ContractError("Request requires exactly question and document_ids")
        question, ids = value["question"], value["document_ids"]
        if not isinstance(question, str) or not question.strip() or len(question) > 500:
            raise ContractError("Question must be nonempty text <= 500 characters")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 8:
            raise ContractError("document_ids must be a list containing 1..8 IDs")
        identifiers = tuple(document_id(item) for item in ids)
        if len(set(identifiers)) != len(identifiers):
            raise ContractError("Document IDs must be unique")
        return cls(question.strip(), identifiers)


@dataclass(frozen=True)
class Document:
    identifier: str
    title: str
    content: str


@dataclass(frozen=True)
class Answer:
    text: str
    source_ids: tuple[str, ...]
    outcome: str

    def wire(self) -> dict:
        value = asdict(self)
        value["source_ids"] = list(self.source_ids)
        return value

    @classmethod
    def parse(cls, value: object) -> "Answer":
        if not isinstance(value, dict) or set(value) != {"text", "source_ids", "outcome"}:
            raise ContractError("Answer requires exactly text, source_ids and outcome")
        text, ids, outcome = value["text"], value["source_ids"], value["outcome"]
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ContractError("Answer text must be nonempty and <= 2000 characters")
        if not isinstance(ids, list) or len(ids) > 8:
            raise ContractError("Answer source_ids must be a list of at most 8 IDs")
        sources = tuple(document_id(item) for item in ids)
        if len(sources) != len(set(sources)):
            raise ContractError("Answer citations must be unique")
        if outcome not in ("answered", "needs_review"):
            raise ContractError("Answer outcome must be answered or needs_review")
        if (outcome == "answered" and not sources) or (outcome == "needs_review" and sources):
            raise ContractError(
                "Answered output needs evidence; abstention has no asserted citations"
            )
        return cls(text, sources, outcome)
