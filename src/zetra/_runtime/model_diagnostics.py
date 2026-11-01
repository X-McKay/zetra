"""Allowlisted model failure metadata; never exception messages or provider payloads."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    reason: Literal[
        "provider_timeout",
        "connection_error",
        "http_error",
        "provider_error",
        "invalid_provider_response",
        "invalid_choices",
        "response_truncated",
        "response_filtered",
        "invalid_finish_reason",
        "parallel_tool_calls",
        "empty_response",
        "unknown_tool",
        "missing_tool_call_id",
        "invalid_tool_arguments",
        "non_definitive_response",
        "trial_timeout",
        "unclassified",
    ]
    http_status: int | None = Field(default=None, ge=100, le=599)

    @model_validator(mode="after")
    def http_status_matches_reason(self):
        if (self.reason == "http_error") != (self.http_status is not None):
            raise ValueError("HTTP status requires an HTTP error")
        return self


class ModelFailureEvidence(ModelDiagnostic):
    boundary_index: int = Field(ge=0, le=63)


class ModelCallError(ValueError):
    def __init__(self, reason, *, http_status=None):
        self.diagnostic = ModelDiagnostic(reason=reason, http_status=http_status)
        super().__init__(self.diagnostic.reason)


def model_failure_evidence(exc, boundary_index):
    """Follow only explicit exception causes, retaining bounded known metadata."""
    cause = exc
    for _ in range(8):
        if isinstance(cause, ModelCallError):
            return ModelFailureEvidence(
                **cause.diagnostic.model_dump(), boundary_index=boundary_index
            ).model_dump()
        cause = cause.__cause__
        if cause is None:
            break
    return ModelFailureEvidence(
        reason="trial_timeout" if isinstance(exc, TimeoutError) else "unclassified",
        boundary_index=boundary_index,
    ).model_dump()
