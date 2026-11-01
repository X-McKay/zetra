"""Fixed-sample statistics; never pool repeated attempts across different cases."""

import math
from statistics import NormalDist, mean, median


def wilson(successes, total, confidence):
    """Two-sided Wilson score interval for independent, stable Bernoulli draws."""
    if (
        type(successes) is not int
        or type(total) is not int
        or not 0 <= successes <= total
        or total < 1
        or type(confidence) not in (int, float)
        or not math.isfinite(confidence)
        or not 0 < confidence < 1
    ):
        raise ValueError("invalid binomial observations or confidence")
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    p = successes / total
    scale = 1 + z * z / total
    center = (p + z * z / (2 * total)) / scale
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / scale
    return {"lower": max(0.0, center - radius), "upper": min(1.0, center + radius)}


def metric_summary(values):
    """Nearest-rank p95 over observed values; missing is explicitly not zero."""
    observed = []
    missing = 0
    for value in values:
        if value is None:
            missing += 1
        elif type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid nonnegative finite metric")
        else:
            observed.append(value)
    observed.sort()
    return {
        "observed": len(observed),
        "missing": missing,
        "total": sum(observed) if observed else None,
        "mean": mean(observed) if observed else None,
        "median": median(observed) if observed else None,
        "p95": observed[math.ceil(len(observed) * 0.95) - 1] if observed else None,
        "maximum": observed[-1] if observed else None,
    }
