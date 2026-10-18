import asyncio

import pytest

from agent_package.agents.document_reader.tools.document_read import read_document


def test_reads_approved_guide():
    assert "verify metadata" in asyncio.run(read_document("guide"))


def test_rejects_other_documents():
    with pytest.raises(ValueError, match="approved guide"):
        asyncio.run(read_document("../secrets"))
