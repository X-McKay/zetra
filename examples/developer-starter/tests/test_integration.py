"""Local tool integration checks, without network or model credentials."""

import shutil
import tempfile
import unittest
from pathlib import Path

from document_assistant.factory import create_application
from document_assistant.models import ContractError
from document_assistant.tools import ApprovedFileReader, DocumentDenied, DocumentUnavailable

ROOT = Path(__file__).resolve().parents[1]


class ToolIntegrationTests(unittest.TestCase):
    def test_allowed_file_and_existing_unapproved_file(self):
        reader = ApprovedFileReader(ROOT / "fixtures/documents", frozenset({"staff-handbook"}))
        self.assertIn("approved document portal", reader.read("staff-handbook").content)
        self.assertTrue((ROOT / "fixtures/documents/private-budget.md").is_file())
        with self.assertRaises(DocumentDenied):
            reader.read("private-budget")

    def test_missing_approved_file_reports_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            reader = ApprovedFileReader(Path(folder).resolve(), frozenset({"missing"}))
            with self.assertRaises(DocumentUnavailable):
                reader.read("missing")

    def test_symlink_does_not_extend_approved_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            outside = root / "outside.md"
            outside.write_text("# Outside\nSynthetic outside content", encoding="utf-8")
            documents = root / "documents"
            documents.mkdir()
            (documents / "staff-handbook.md").symlink_to(outside)
            reader = ApprovedFileReader(documents, frozenset({"staff-handbook"}))
            with self.assertRaises(DocumentDenied):
                reader.read("staff-handbook")

    def test_oversized_and_invalid_utf8_documents_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            target = root / "staff-handbook.md"
            reader = ApprovedFileReader(root, frozenset({"staff-handbook"}))
            for content in (b"x" * 20001, b"\xff"):
                target.write_bytes(content)
                with self.subTest(size=len(content)), self.assertRaises(DocumentUnavailable):
                    reader.read("staff-handbook")

    def test_invalid_construction_config_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            shutil.copytree(ROOT, root, dirs_exist_ok=True)
            config = root / "application.toml"
            config.write_text(
                config.read_text().replace("max_documents = 2", "max_documents = true"),
                encoding="utf-8",
            )
            with self.assertRaises(ContractError):
                create_application(root)
