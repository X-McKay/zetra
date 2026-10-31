import asyncio

import pytest

from agent_package.agents.document_reader.tools.document_read import (
    read_document,
)


def test_reads_approved_guide():
    assert "verify metadata" in asyncio.run(read_document("guide"))


@pytest.mark.parametrize(
    "document_id",
    ["", "GUIDE", "other", "../secrets", "guide/../secrets", "/etc/passwd", "C:\\secrets"],
)
def test_rejects_other_documents(document_id):
    with pytest.raises(ValueError, match="approved guide"):
        asyncio.run(read_document(document_id))


def test_invalid_request_does_not_prevent_later_approved_read():
    async def invalid_then_valid():
        with pytest.raises(ValueError, match="approved guide"):
            await read_document("../secrets")
        return await read_document("guide")

    assert asyncio.run(invalid_then_valid()) == (
        "The approved guide says: verify metadata, run tests, and review the result."
    )
