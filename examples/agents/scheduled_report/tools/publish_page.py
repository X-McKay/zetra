from zetra.sdk import tool

from ..contracts import ReportRequest, WikiPage


@tool("wiki.write")  # reversible: listed under [reversible] in zetra.toml, so no human approval
def publish_page(body: str, *, request: ReportRequest) -> WikiPage:
    """Publish the daily report page. The title is derived from the scheduled day, not chosen by the model."""
    return WikiPage(title=f"Service report {request.day.isoformat()}", body=body)
