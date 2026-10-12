"""Error types shared by the SDK. Codes follow the proposal's ZT-* scheme."""

from __future__ import annotations


class CompileError(ValueError):
    """A package fails a build rule. ``code`` names the rule; the message says what to do."""

    def __init__(self, code: str, message: str, *, where: str | None = None):
        self.code, self.where = code, where
        super().__init__(f"[{code}] {message}" + (f" ({where})" if where else ""))


class Denied(PermissionError):
    """A runtime control refused an operation."""
