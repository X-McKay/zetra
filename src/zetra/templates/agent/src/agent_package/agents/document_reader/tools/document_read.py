"""A deliberately small approved document boundary."""


async def read_document(document_id: str) -> str:
    """Read the approved guide by its stable document identifier."""
    if document_id != "guide":
        raise ValueError("only the approved guide document is available")
    return "The approved guide says: verify metadata, run tests, and review the result."
