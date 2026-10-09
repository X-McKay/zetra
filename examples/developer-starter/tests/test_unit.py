"""Unit checks: malformed contracts and unsupported adapter citations fail."""

import unittest
from pathlib import Path

from document_assistant.dependencies import Dependencies
from document_assistant.factory import DocumentAssistant
from document_assistant.models import Answer, ContractError, Document, Request

ROOT = Path(__file__).resolve().parents[1]


class OneDocumentReader:
    def read(self, identifier: str) -> Document:
        return Document(identifier, "Title", "Approved statement")


class UnsupportedCitationAnswerer:
    def answer(self, question: str, documents: tuple[Document, ...], instructions: str) -> Answer:
        return Answer("A claim without supplied evidence", ("private-budget",), "answered")


class ContractTests(unittest.TestCase):
    def test_request_rejects_invalid_input(self):
        invalid = [
            None,
            {"question": "", "document_ids": ["staff-handbook"]},
            {"question": "x", "document_ids": ["../private-budget"]},
            {"question": "x", "document_ids": ["staff-handbook", "staff-handbook"]},
            {"question": "x", "document_ids": "staff-handbook"},
            {"question": "x", "document_ids": ["staff-handbook"], "permission": "all"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ContractError):
                Request.parse(value)

    def test_answer_requires_evidence_or_abstention(self):
        invalid = [
            {"text": "Claim", "source_ids": [], "outcome": "answered"},
            {"text": "Review", "source_ids": ["staff-handbook"], "outcome": "needs_review"},
            {"text": "Claim", "source_ids": ["staff-handbook"], "outcome": "approved"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ContractError):
                Answer.parse(value)

    def test_adapter_cannot_cite_unsupplied_document(self):
        application = DocumentAssistant(
            Dependencies(OneDocumentReader(), UnsupportedCitationAnswerer(), "Use evidence", 1)
        )
        with self.assertRaisesRegex(ContractError, "not supplied"):
            application.run({"question": "Claim?", "document_ids": ["staff-handbook"]})

    def test_configured_request_limit_checked_before_read(self):
        application = DocumentAssistant(
            Dependencies(OneDocumentReader(), UnsupportedCitationAnswerer(), "Use evidence", 1)
        )
        with self.assertRaisesRegex(ContractError, "document limit"):
            application.run(
                {"question": "Claim?", "document_ids": ["staff-handbook", "expense-policy"]}
            )
