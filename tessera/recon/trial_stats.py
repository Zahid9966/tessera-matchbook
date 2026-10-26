"""Statistics for judging accuracy from a labelled SAMPLE. Real statements have no ground truth, so the honest way to
measure matching accuracy is to label a random sample and report an interval, never a bare percentage."""
from __future__ import annotations
import math

Z95 = 1.959964

def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a proportion k/n. Sensible at k=0 and small n (unlike the textbook normal interval)."""
    if n <= 0: return (0.0, 1.0)
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n); w = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - w) / d), min(1.0, (c + w) / d))

def upper_bound_zero(n: int, conf: float = 0.95) -> float:
    """Exact one-sided upper bound when 0 events were seen in n trials (the 'rule of three' is its approximation 3/n)."""
    return 1.0 if n <= 0 else 1 - (1 - conf) ** (1.0 / n)

def samples_needed(max_rate: float, conf: float = 0.95) -> int:
    """Clean samples (zero wrong) needed before you can claim the true wrong-rate is below max_rate with the given confidence."""
    return math.ceil(math.log(1 - conf) / math.log(1 - max_rate))

def stratified_rate(strata: list[dict], z: float = Z95) -> dict:
    """Combine per-stratum samples into one population estimate.
    strata: [{N: population size, n: labelled sample size, k: events (e.g. wrong matches) in the sample}, ...]
    Point estimate = sum(N_h/N * k_h/n_h). The interval uses the stratified variance with a finite-population correction, and a
    conservative upper bound (exact zero-event bound per stratum) so that a clean sample never produces a falsely tight interval."""
    N = sum(s["N"] for s in strata); usable = [s for s in strata if s["n"] > 0 and s["N"] > 0]
    if not N or not usable: return {"estimate": None, "low": None, "high": None, "coverage": 0.0}
    covered = sum(s["N"] for s in usable); est = sum(s["N"] / covered * s["k"] / s["n"] for s in usable)
    var = sum((s["N"] / covered) ** 2 * (s["k"] / s["n"]) * (1 - s["k"] / s["n"]) / s["n"] * (1 - s["n"] / s["N"]) for s in usable)
    se = math.sqrt(max(var, 0.0))
    # conservative upper bound: per-stratum one-sided bounds (exact when k=0), weighted
    upper = sum(s["N"] / covered * (upper_bound_zero(s["n"]) if s["k"] == 0 else wilson(s["k"], s["n"], z)[1]) for s in usable)
    return {"estimate": est, "low": max(0.0, est - z * se), "high": max(est + z * se, upper) if se > 0 or any(s["k"] == 0 for s in usable) else est,
            "conservative_upper": upper, "coverage": covered / N}
