import math

import pytest

from zetra import eval_statistics as stats


@pytest.mark.parametrize(
    "successes,total,expected",
    [(5, 10, (0.236593, 0.763407)), (0, 10, (0, 0.277533)), (10, 10, (0.722467, 1))],
)
def test_wilson_reference_bounds(successes, total, expected):
    interval = stats.wilson(successes, total, 0.95)
    assert interval["lower"] == pytest.approx(expected[0], abs=0.000001)
    assert interval["upper"] == pytest.approx(expected[1], abs=0.000001)


@pytest.mark.parametrize(
    "args",
    [(True, 10, 0.95), (11, 10, 0.95), (-1, 10, 0.95), (0, 0, 0.95), (1, 2, math.nan), (1, 2, 1)],
)
def test_invalid_binomial_observations_rejected(args):
    with pytest.raises(ValueError):
        stats.wilson(*args)


def test_missing_metrics_are_not_free_usage_or_zero_latency():
    summary = stats.metric_summary([10, None, 20, 100])
    assert summary == {
        "observed": 3,
        "missing": 1,
        "total": 130,
        "mean": pytest.approx(130 / 3),
        "median": 20,
        "p95": 100,
        "maximum": 100,
    }
    assert stats.metric_summary([None, None]) == {
        "observed": 0,
        "missing": 2,
        "total": None,
        "mean": None,
        "median": None,
        "p95": None,
        "maximum": None,
    }


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "3"])
def test_malformed_metrics_are_not_summarized(value):
    with pytest.raises(ValueError):
        stats.metric_summary([value])
