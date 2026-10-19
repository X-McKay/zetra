import asyncio

import pytest
from document_reader.agents.document_reader.tools.document_read import read_document


def test_approved_document_only():
    assert asyncio.run(read_document("guide"))["document"] == "guide"
    with pytest.raises(ValueError):
        asyncio.run(read_document("../private"))
