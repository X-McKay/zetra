"""Curated read-only file tool. This helper is not an OS isolation boundary."""

from pathlib import Path

from .models import ContractError, Document, document_id


class DocumentDenied(PermissionError):
    """The requested document is outside the curator's approved IDs."""


class DocumentUnavailable(OSError):
    """Approved evidence is missing, oversized or unreadable."""


class ApprovedFileReader:
    def __init__(self, root: Path, allowed_ids: frozenset[str]):
        if root.is_symlink() or not root.is_dir():
            raise ContractError("Approved document root must be a real directory")
        self.root = root.resolve()
        self.allowed_ids = frozenset(document_id(item) for item in allowed_ids)

    def read(self, identifier: str) -> Document:
        identifier = document_id(identifier)
        if identifier not in self.allowed_ids:
            raise DocumentDenied("Document is not in the approved ID list")
        path = self.root / (identifier + ".md")
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise DocumentDenied("Document path escapes its approved root")
        try:
            if path.stat().st_size > 20000:
                raise DocumentUnavailable("Approved document exceeds 20 KB")
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise DocumentUnavailable("Approved document could not be read") from exc
        lines = content.splitlines()
        if not lines or not lines[0].startswith("# ") or not content.strip():
            raise DocumentUnavailable("Approved fixture requires a Markdown title and content")
        return Document(identifier, lines[0][2:], "\n".join(lines[1:]).strip())
