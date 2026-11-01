import pytest

from zetra.eval_reports import TrialEvidence


def unknown_trial():
    return {
        "trial": 0,
        "outcome": "error",
        "status": "OUTCOME_UNKNOWN",
        "error_type": "OutcomeUnknown",
        "assertions": [],
        "tools": {},
        "metrics": {"steps": 0, "duration_ms": 10.0, "input_tokens": None, "output_tokens": None},
    }


def test_historical_trial_without_diagnostic_serializes_identically():
    original = unknown_trial()
    assert TrialEvidence.model_validate(original).model_dump() == original


@pytest.mark.parametrize(
    "change",
    [
        {"message": "private provider response"},
        {"reason": "private arbitrary exception"},
        {"http_status": True},
        {"http_status": 700},
        {"boundary_index": -1},
        {"reason": "provider_timeout", "http_status": 503},
        {"http_status": None},
    ],
)
def test_diagnostics_reject_unbounded_or_incoherent_metadata(change):
    data = unknown_trial()
    data["diagnostic"] = {"reason": "http_error", "http_status": 503, "boundary_index": 0, **change}
    with pytest.raises(ValueError):
        TrialEvidence.model_validate(data)


@pytest.mark.parametrize("outcome,status", [("passed", "SUCCEEDED"), ("not_run", None)])
def test_model_diagnostic_cannot_be_attached_to_success_or_unrun_trial(outcome, status):
    data = {
        **unknown_trial(),
        "outcome": outcome,
        "status": status,
        "diagnostic": {"reason": "provider_timeout", "boundary_index": 0},
    }
    with pytest.raises(ValueError, match="diagnostic"):
        TrialEvidence.model_validate(data)
