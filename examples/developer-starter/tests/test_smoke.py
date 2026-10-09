"""Smoke check: configuration, factory and a supported local query work."""

import unittest
from pathlib import Path

from document_assistant.factory import create_application

ROOT = Path(__file__).resolve().parents[1]


class SmokeTests(unittest.TestCase):
    def test_factory_and_supported_question(self):
        result = create_application(ROOT).run(
            {"question": "Where can I find the staff handbook?", "document_ids": ["staff-handbook"]}
        )
        self.assertEqual(
            result,
            {
                "text": "The staff handbook is available in the approved document portal.",
                "source_ids": ["staff-handbook"],
                "outcome": "answered",
            },
        )
