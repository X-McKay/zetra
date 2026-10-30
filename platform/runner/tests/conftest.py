from __future__ import annotations

import logging

import pytest

from runner import activities as act
from runner.types import TOKEN_PATTERNS


@pytest.fixture(autouse=True)
def no_tokens_in_logs(caplog):
    """Section 13.6: no captured log line may look like a JWT or a bearer credential."""
    caplog.set_level(logging.DEBUG)
    yield
    for rec in caplog.records:
        line = rec.getMessage()
        assert not any(p.search(line) for p in TOKEN_PATTERNS), line


@pytest.fixture(autouse=True)
def reset_deps():
    yield
    act.configure(None)
