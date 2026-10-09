"""Typed wire contracts; observed text cannot select arbitrary tools or services."""

import re
from dataclasses import asdict, dataclass

from zetra.runtime import ActionStore, Denied, action_digest


@dataclass(frozen=True)
class Incident:
    incidentId: str
    service: str
    severity: str
    summary: str

    @classmethod
    def parse(cls, value: object) -> "Incident":
        if not isinstance(value, dict) or set(value) != {
            "incidentId",
            "service",
            "severity",
            "summary",
        }:
            raise Denied("Incident requires incidentId, service, severity and summary")
        if not isinstance(value["incidentId"], str) or not re.fullmatch(
            r"INC-[0-9]{4}-[0-9]{4}", value["incidentId"]
        ):
            raise Denied("Incident ID must be INC-YYYY-NNNN")
        if (
            value["service"] != "checkout-api"
            or not isinstance(value["severity"], str)
            or value["severity"] not in {"warning", "critical"}
        ):
            raise Denied(
                "This reviewed reference supports checkout-api warning/critical incidents only"
            )
        if (
            not isinstance(value["summary"], str)
            or not value["summary"].strip()
            or len(value["summary"]) > 500
        ):
            raise Denied("Incident summary must be nonempty text <= 500 characters")
        return cls(**value)


@dataclass(frozen=True)
class Proposal:
    incidentId: str
    service: str
    runId: str
    releaseId: str
    operationId: str
    observedStatus: str
    runbookKey: str
    ticketPayload: dict[str, str]
    actionDigest: str

    def wire(self) -> dict:
        return asdict(self)

    @classmethod
    def parse(cls, value: object) -> "Proposal":
        required = {
            "incidentId",
            "service",
            "runId",
            "releaseId",
            "operationId",
            "observedStatus",
            "runbookKey",
            "ticketPayload",
            "actionDigest",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise Denied("Proposal fields do not match the reviewed contract")
        if any(
            not isinstance(value[key], str) or not value[key] or len(value[key]) > 256
            for key in required - {"ticketPayload"}
        ):
            raise Denied("Proposal identifiers must be bounded nonempty strings")
        if (
            not re.fullmatch(r"INC-[0-9]{4}-[0-9]{4}", value["incidentId"])
            or value["service"] != "checkout-api"
        ):
            raise Denied("Proposal incident/service does not match the reference scope")
        if value["operationId"] != value["incidentId"] + ":ticket:1":
            raise Denied("Business operation identity must be stable across delivery attempts")
        if (
            value["observedStatus"] not in {"healthy", "degraded"}
            or value["runbookKey"] != "checkout-degradation"
        ):
            raise Denied("Proposal observation/runbook does not match reviewed values")
        ActionStore.validate_payload(value["ticketPayload"])
        if value["actionDigest"] != action_digest(value["ticketPayload"]):
            raise Denied("Proposal payload digest mismatch")
        return cls(**value)
