"""客群结构、季节与规模调整：把“可比”从日期扩展到构成。

拟合的是每月内部差异（用同一月份内的对照抵消季节和共同冲击），再按网格
人口加权。协变量只使用计划的、启动前可观测的结构信息（城乡类型、规模），
不使用结果本身，因此不会把不利网格筛掉。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # avoid a runtime import cycle with engine
    from .engine import ArmObservation

def _solve(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    n = len(vector)
    aug = [list(matrix[i]) + [vector[i]] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-10:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        lead = aug[col][col]
        aug[col] = [value / lead for value in aug[col]]
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            if factor:
                aug[row] = [a - factor * b for a, b in zip(aug[row], aug[col])]
    return [aug[i][n] for i in range(n)]


def weighted_lm(
    observations: "list[ArmObservation]", covariates: list[str]
) -> dict[str, Any]:
    """One weighted least-squares fit with month fixed effects.

    Returns the treated coefficient and its conventional standard error.  The
    interval built from it treats grid-months as independent, so it is narrower
    than a cluster interval and the report labels it accordingly.
    """

    rows = [o for o in observations if o.arm in ("treated", "control")]
    if not rows:
        return {"ok": False, "reason": "没有可用观测"}
    months = sorted({o.month for o in rows})
    if len(months) < 2:
        return {"ok": False, "reason": "月份过少，无法分离月份效应与措施"}
    if len({o.arm for o in rows}) < 2:
        return {"ok": False, "reason": "缺少实施或对照一侧"}
    month_index = {m: i for i, m in enumerate(months)}
    names: list[str] = []
    for cov in covariates:
        names.append(cov)
    names.append("treated")
    names.extend(f"month:{m}" for m in months[1:])
    n_params = len(names) + 1  # intercept

    def row_vector(obs: "ArmObservation") -> list[float]:
        vec = [1.0]
        if "urban" in covariates:
            vec.append(1.0 if obs.area_type == "urban" else 0.0)
        if "rural" in covariates:
            vec.append(1.0 if obs.area_type == "rural" else 0.0)
        if "size" in covariates:
            vec.append(math.log(max(obs.population, 1)))
        vec.append(1.0 if obs.arm == "treated" else 0.0)
        for m in months[1:]:
            vec.append(1.0 if obs.month == m else 0.0)
        return vec

    xtwx = [[0.0] * n_params for _ in range(n_params)]
    xtwy = [0.0] * n_params
    for obs in rows:
        vec = row_vector(obs)
        w = max(obs.weight, 1e-9)
        for i in range(n_params):
            if vec[i] == 0.0:
                continue
            xtwy[i] += vec[i] * w * obs.y
            for j in range(n_params):
                if vec[j]:
                    xtwx[i][j] += vec[i] * w * vec[j]
    beta = _solve(xtwx, xtwy)
    if beta is None:
        return {"ok": False, "reason": "设计矩阵不可逆（例如某类型网格只出现在一侧）"}
    treated_pos = 1 + covariates_count(covariates)
    coef = beta[treated_pos]
    # conventional (non-clustered) standard error
    dof = len(rows) - n_params
    if dof <= 0:
        return {"ok": False, "reason": "观测数少于参数数", "coef": coef, "names": names}
    residual = 0.0
    for obs in rows:
        vec = row_vector(obs)
        pred = sum(b * v for b, v in zip(beta, vec))
        w = max(obs.weight, 1e-9)
        residual += w * (obs.y - pred) ** 2
    sigma2 = residual / dof
    inverse = _invert(xtwx)
    if inverse is None:
        return {"ok": False, "reason": "协方差矩阵不可求逆", "coef": coef, "names": names}
    se = math.sqrt(max(sigma2 * inverse[treated_pos][treated_pos], 0.0))
    return {
        "ok": True,
        "coef": coef,
        "se": se,
        "dof": dof,
        "names": names,
        "n_observations": len(rows),
        "n_grids": len({o.grid for o in rows}),
        "months": months,
        "covariates": list(covariates),
    }


def covariates_count(covariates: list[str]) -> int:
    return len(covariates)


def _invert(matrix: list[list[float]]) -> list[list[float]] | None:
    n = len(matrix)
    aug = [list(matrix[i]) + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-10:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        lead = aug[col][col]
        aug[col] = [value / lead for value in aug[col]]
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            if factor:
                aug[row] = [a - factor * b for a, b in zip(aug[row], aug[col])]
    return [row[n:] for row in aug]


def adjusted_estimate(
    observations: "list[ArmObservation]", confidence: float
) -> dict[str, Any]:
    from .stats import normal_quantile

    base = weighted_lm(observations, [])
    full = weighted_lm(observations, ["urban", "rural", "size"])
    out: dict[str, Any] = {"unadjusted": base, "adjusted": full}
    for key, fit in out.items():
        if fit.get("ok") and fit.get("se") is not None:
            z = normal_quantile(1 - (1 - confidence) / 2)
            fit["ci_low"] = fit["coef"] - z * fit["se"]
            fit["ci_high"] = fit["coef"] + z * fit["se"]
            fit["ci_method"] = "回归（月份固定效应）常规标准误；未按网格聚类，区间偏窄"
    return out
