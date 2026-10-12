from typing import Literal

from zetra.sdk import approval, tool

from ..contracts import Alert, Page


@approval(ttl="PT10M")  # a page is urgent: the approval request expires in ten minutes
@tool("pager.write")  # a write: the body builds the payload; the receiver 'pager' commits it
def page_oncall(urgency: Literal["high", "low"], summary: str, *, alert: Alert) -> Page:
    """Page the on-call engineer for this alert's service."""
    # `alert` is filled by the SDK from the run's Input and hidden from the model,
    # so the service being paged can never be chosen by the model.
    return Page(service=alert.service, urgency=urgency, summary=summary)
