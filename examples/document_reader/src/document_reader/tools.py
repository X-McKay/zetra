async def read_document(document: str) -> dict:
    """Read the approved guide by its identifier."""
    if document != "guide":
        raise ValueError("document is not approved")
    return {"document": document, "text": "Sessions use private workspaces and confirmed cleanup."}
