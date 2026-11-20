"""Small-sample statistics helpers (standard library only).

The analysis has few grids, so inference is cluster-based: each grid counts as
one independent unit and no grid is dropped.  Where a cluster interval cannot
be formed, the naive binomial interval is still shown but explicitly labelled
as understating uncertainty.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def normal_quantile(p: float) -> float:
    if not 0 < p < 1:
        raise ValueError("p 必须在 (0,1)")
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if normal_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _betacf(a: float, b: float, x: float) -> float:
    max_iter = 300
    eps = 3e-14
    tiny = 1e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def regularized_beta(x: float, a: float, b: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_bt = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    beta = math.exp(log_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return beta * _betacf(a, b, x) / a
    return 1.0 - beta * _betacf(b, a, 1.0 - x) / b


def student_t_cdf(t: float, df: float) -> float:
    if df <= 0:
        raise ValueError("df 必须为正")
    x = df / (df + t * t)
    tail = 0.5 * regularized_beta(x, df / 2.0, 0.5)
    return tail if t < 0 else 1.0 - tail


def student_t_quantile(p: float, df: float) -> float:
    if not 0 < p < 1:
        raise ValueError("p 必须在 (0,1)")
    if df > 500:
        return normal_quantile(p)
    lo, hi = -200.0, 200.0
    for _ in range(300):
        mid = (lo + hi) / 2
        if student_t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def wilson_interval(successes: int, total: int, confidence: float) -> tuple[float, float]:
    if total <= 0:
        return (0.0, 1.0)
    z = normal_quantile(1 - (1 - confidence) / 2)
    phat = successes / total
    denom = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denom
    half = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float:
    total = sum(weights)
    if total <= 0:
        return float("nan")
    return sum(v * w for v, w in zip(values, weights)) / total


def jackknife_se(estimates: Sequence[float]) -> tuple[float, float]:
    """Return (mean, standard error) of leave-one-cluster-out estimates."""

    n = len(estimates)
    if n < 2:
        return (estimates[0] if estimates else float("nan"), float("nan"))
    mean = sum(estimates) / n
    variance = (n - 1) / n * sum((value - mean) ** 2 for value in estimates)
    return mean, math.sqrt(max(variance, 0.0))


def mean_ci(
    values: Iterable[float], confidence: float
) -> tuple[float, float, float, int] | None:
    values = list(values)
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    se = sd / math.sqrt(n)
    if se == 0:
        return (mean, mean, mean, n)
    t = student_t_quantile(1 - (1 - confidence) / 2, n - 1)
    return (mean, mean - t * se, mean + t * se, n)
