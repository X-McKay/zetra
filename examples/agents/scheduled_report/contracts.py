from datetime import date
from typing import Literal

from pydantic import Field

from zetra.sdk import Contract


class ReportRequest(Contract):
    day: date  # the nominal day, supplied by the schedule trigger, never by the model


class WikiPage(Contract):  # a reversible write: a page can be re-published or deleted
    title: str = Field(max_length=120)
    body: str = Field(max_length=8000)


class ReportResult(Contract):
    kind: Literal["published"] = "published"
    page_title: str


Input, Output = ReportRequest, ReportResult
