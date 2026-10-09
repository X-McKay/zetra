"""Inject typed interfaces. The application does not choose filesystem roots."""

from dataclasses import dataclass
from typing import Protocol

from .models import Answer, Document


class DocumentReader(Protocol):
    def read(self, identifier: str) -> Document: ...


class Answerer(Protocol):
    def answer(
        self, question: str, documents: tuple[Document, ...], instructions: str
    ) -> Answer: ...


@dataclass(frozen=True)
class Dependencies:
    documents: DocumentReader
    answerer: Answerer
    instructions: str
    max_documents: int
