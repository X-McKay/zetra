from typing import Literal

from pydantic import Field

from zetra.sdk import Contract  # BaseModel: extra="forbid", frozen


class Alert(Contract):
    alert_id: str = Field(pattern=r"^ALR-[0-9]{8}$", examples=["ALR-20261012"])
    service: str = Field(max_length=64, examples=["checkout-api"])
    severity: Literal["info", "warning", "critical"]
    message: str = Field(max_length=2000)  # untrusted text from the alerting system


class Page(
    Contract
):  # the exact payload the on-call human approves and the 'pager' receiver commits
    service: str
    urgency: Literal["high", "low"]
    summary: str = Field(max_length=500)


class Annotation(Contract):  # committed by the 'alerts' receiver without approval (reversible)
    alert_id: str
    note: str = Field(max_length=1000)


class Paged(Contract):
    kind: Literal["paged"] = "paged"
    reason: str


class NoAction(Contract):  # "do nothing" is a typed outcome, not an error
    kind: Literal["no_action"] = "no_action"
    reason: str


Input, Output = Alert, Paged | NoAction
