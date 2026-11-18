"""The decisions, each a function of the store. A rule that says no raises `Refusal`."""

from __future__ import annotations


class Refusal(Exception):
    """A rule refuses a request. `code` is one of the codes of `api/errors.py`; the HTTP layer
    turns it into the error body. Raised inside a transaction, it leaves nothing written."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
