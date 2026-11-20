"""Estimates, uncertainty, sensitivity and applicability.

Primary quantity: within each comparison month, the matured-weighted
difference in timely-completion rate between grids already exposed and grids
that are still eligible as not-yet-started controls, aggregated over months
with matured-case weights.  Its uncertainty is a leave-one-grid-out jackknife
interval, so an interval exists only when both arms retain data after dropping
one grid; otherwise the report says the interval cannot be estimated.  A plain
pooled-count interval is shown alongside it, labelled as understating
uncertainty.

Nothing here selects or filters grids by their results.  Variants differ only
by pre-declared structural rules (weighting, control source, shock handling,
near-start controls, transition months, baseline adjustment).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

from . import design
from .domain import (
    check_sources,
    compute_exposure,
    display_months,
    index_by,
    month_end,
    month_start,
    parse_date,
    shift_month,
    sources_fingerprint,
)
from .adjust import adjusted_estimate
from .stats import mean_ci, student_t_quantile, wilson_interval

SENTINEL_VERSION = "1.0"


@dataclass
class ArmObservation:
    grid: str
    month: str
    arm: str
    timely: int
    matured: int
    received: int
    population: int
    area_type: str
    weight: float
    y: float
    intensity: str
    status: str
    distance_to_start: int | None
    shocks: tuple[str, ...]
    active_fraction: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "grid": self.grid,
            "month": self.month,
            "arm": self.arm,
            "timely": self.timely,
            "matured": self.matured,
            "received": self.received,
            "population": self.population,
            "area_type": self.area_type,
            "weight": round(self.weight, 6),
            "y": self.y,
            "intensity": self.intensity,
            "status": self.status,
            "distance_to_start": self.distance_to_start,
            "shocks": list(self.shocks),
            "active_fraction": self.active_fraction,
        }


@dataclass
class Estimate:
    status: str
    status_label: str
    value: float | None
    ci_low: float | None
    ci_high: float | None
    ci_method: str
    naive_low: float | None = None
    naive_high: float | None = None
    se: float | None = None
    df: int | None = None
    months_used: list[str] = field(default_factory=list)
    treated_grids: list[str] = field(default_factory=list)
    control_grids: list[str] = field(default_factory=list)
    treated_matured: int = 0
    control_matured: int = 0
    month_estimates: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    jk_values: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        def r(value: float | None) -> float | None:
            return None if value is None else round(value, 6)

        return {
            "status": self.status,
            "status_label": self.status_label,
            "value": r(self.value),
            "value_pct": None if self.value is None else round(self.value * 100, 2),
            "ci_low": r(self.ci_low),
            "ci_high": r(self.ci_high),
            "ci_low_pct": None if self.ci_low is None else round(self.ci_low * 100, 2),
            "ci_high_pct": None if self.ci_high is None else round(self.ci_high * 100, 2),
            "ci_method": self.ci_method,
            "naive_low": r(self.naive_low),
            "naive_high": r(self.naive_high),
            "naive_low_pct": None if self.naive_low is None else round(self.naive_low * 100, 2),
            "naive_high_pct": None if self.naive_high is None else round(self.naive_high * 100, 2),
            "se": r(self.se),
            "df": self.df,
            "months_used": self.months_used,
            "treated_grids": self.treated_grids,
            "control_grids": self.control_grids,
            "treated_matured": self.treated_matured,
            "control_matured": self.control_matured,
            "month_estimates": self.month_estimates,
            "notes": self.notes,
            "jackknife_values": self.jk_values,
        }


def _distance_to_start(timeline: design.GridTimeline, month: str) -> int | None:
    return design.months_until_start(timeline, month)


def _blocked_cells(tables: dict[str, list[dict[str, Any]]]) -> set[tuple[str, str]]:
    return {
        (item.get("grid", ""), item.get("month", ""))
        for item in check_sources(tables)
        if item["severity"] == "warning" and item.get("grid") and item.get("month")
    }


def build_observations(
    tables: dict[str, list[dict[str, Any]]],
    audit: dict[str, Any],
    protocol: dict[str, Any],
    *,
    weighting: str,
    control_source: str,
    exclude_shock: bool = False,
    exclude_soon: bool = False,
    include_partial: bool | None = None,
) -> list[ArmObservation]:
    blocked = _blocked_cells(tables)
    outcomes = {
        key: row
        for key, row in index_by(
            tables["outcomes"], lambda row: (row["grid"], row["month"])
        ).items()
        if key not in blocked
    }
    shocks: dict[tuple[str, str], list[str]] = {}
    for shock in tables["shocks"]:
        shocks.setdefault((shock["grid"], shock["month"]), []).append(shock["type"])

    timelines = audit["timelines"]
    observations: list[ArmObservation] = []
    include_partial = (
        protocol["partial_exposure_policy"] == "include_as_treated"
        if include_partial is None
        else include_partial
    )
    treated_statuses = {"treated", "partial"} if include_partial else {"treated"}

    for row in audit["rows"]:
        if row["month"] not in audit["comparison_months"]:
            continue
        record = outcomes.get((row["grid"], row["month"]))
        if record is None or record["matured"] <= 0:
            continue
        month_shocks = tuple(shocks.get((row["grid"], row["month"]), ()))
        if exclude_shock and month_shocks:
            continue
        timeline = timelines[row["grid"]]
        if row["status"] in treated_statuses:
            arm = "treated"
        elif row["control_eligible"]:
            arm = "control"
            if control_source == "never_started" and timeline.start_date is not None:
                continue
            if exclude_soon:
                gap = _distance_to_start(timeline, row["month"])
                if gap is not None and gap <= design.CONTROL_MONTHS_BEFORE_START_THRESHOLD:
                    continue
        else:
            continue
        base = max(timeline.population, 1) if weighting == "population" else 1.0
        exposure = timeline.exposure[row["month"]]
        active_fraction = (
            exposure.active_days / exposure.days_in_month if exposure.days_in_month else 0.0
        )
        if arm == "treated" and include_partial:
            fraction = active_fraction
        else:
            fraction = 1.0
        observations.append(
            ArmObservation(
                grid=row["grid"],
                month=row["month"],
                arm=arm,
                timely=record["timely"],
                matured=record["matured"],
                received=record["received"],
                population=timeline.population,
                area_type=timeline.area_type,
                weight=base * record["matured"] * fraction,
                y=record["timely"] / record["matured"],
                intensity=exposure.intensity,
                status=row["status"],
                distance_to_start=_distance_to_start(timeline, row["month"]),
                shocks=month_shocks,
                active_fraction=active_fraction,
            )
        )
    return observations


def _arm_month(
    observations: list[ArmObservation], month: str, arm: str
) -> dict[str, Any] | None:
    rows = [o for o in observations if o.month == month and o.arm == arm]
    if not rows:
        return None
    total_weight = sum(o.weight for o in rows)
    if total_weight <= 0:
        return None
    rate = sum(o.y * o.weight for o in rows) / total_weight
    return {
        "month": month,
        "arm": arm,
        "rate": rate,
        "matured": sum(o.matured for o in rows),
        "timely": sum(o.timely for o in rows),
        "received": sum(o.received for o in rows),
        "grids": sorted({o.grid for o in rows}),
        "weight": total_weight,
    }


def month_estimates(observations: list[ArmObservation]) -> list[dict[str, Any]]:
    months = sorted({o.month for o in observations})
    out = []
    for month in months:
        treated = _arm_month(observations, month, "treated")
        control = _arm_month(observations, month, "control")
        if not treated or not control:
            out.append(
                {
                    "month": month,
                    "estimated": False,
                    "treated": treated,
                    "control": control,
                    "reason": "当月缺少实施观测" if not treated else "当月已无合格对照",
                }
            )
            continue
        diff = treated["rate"] - control["rate"]
        weight = treated["matured"] + control["matured"]
        out.append(
            {
                "month": month,
                "estimated": True,
                "treated": treated,
                "control": control,
                "diff": diff,
                "weight": weight,
                "reason": "",
            }
        )
    return out


def _pooled_naive_interval(
    observations: list[ArmObservation], confidence: float
) -> tuple[float | None, float | None]:
    treated = [o for o in observations if o.arm == "treated"]
    control = [o for o in observations if o.arm == "control"]
    if not treated or not control:
        return None, None
    n1 = sum(o.matured for o in treated)
    n0 = sum(o.matured for o in control)
    if n1 <= 0 or n0 <= 0:
        return None, None
    p1 = sum(o.timely for o in treated) / n1
    p0 = sum(o.timely for o in control) / n0
    var = p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0
    half = math.sqrt(max(var, 0.0)) * 1.96 if False else None
    # normal approximation with the pre-specified confidence level
    from .stats import normal_quantile

    z = normal_quantile(1 - (1 - confidence) / 2)
    half = z * math.sqrt(var)
    return p1 - p0 - half, p1 - p0 + half


def _no_control_detail(
    observations: list[ArmObservation], months: list[dict[str, Any]]
) -> list[str]:
    detail = []
    for month in months:
        treated = month["treated"]
        if treated and not month["control"]:
            detail.append(
                f"{month['month']} 有实施观测（{ '、'.join(treated['grids']) }），"
                "但同期没有合格对照"
            )
    return detail


def _estimate_from(
    observations: list[ArmObservation], protocol: dict[str, Any]
) -> Estimate:
    months = month_estimates(observations)
    usable = [m for m in months if m["estimated"]]
    treated_grids = sorted({o.grid for o in observations if o.arm == "treated"})
    control_grids = sorted({o.grid for o in observations if o.arm == "control"})
    treated_matured = sum(o.matured for o in observations if o.arm == "treated")
    control_matured = sum(o.matured for o in observations if o.arm == "control")
    confidence = protocol["confidence_level"]

    if not treated_grids and not control_grids:
        return Estimate(
            "no_data",
            "比较时段内没有可用的实施或对照观测（可能因资料口径问题被排除，见资料核查）",
            None,
            None,
            None,
            "无",
        )
    if not treated_grids:
        return Estimate("no_treated", "比较时段内没有已实施且已成熟的观测", None, None, None,
                        "无")
    no_control_detail = _no_control_detail(observations, months)
    if not control_grids:
        notes = []
        if control_matured:
            notes.append(
                f"对照侧只有 {control_matured} 件成熟事项，不足以构成有效比较"
            )
        else:
            notes.append("比较时段内没有可用的成熟对照观测")
        notes += no_control_detail
        return Estimate(
            "no_control",
            "比较时段内已没有合格的未实施对照，不能估计差异",
            None,
            None,
            None,
            "无",
            treated_grids=treated_grids,
            treated_matured=treated_matured,
            control_matured=control_matured,
            month_estimates=months,
            notes=notes,
        )
    if not usable:
        return Estimate(
            "no_common_month",
            "没有同时具备实施与合格对照的月份，不能估计差异",
            None,
            None,
            None,
            "无",
            treated_grids=treated_grids,
            control_grids=control_grids,
            treated_matured=treated_matured,
            control_matured=control_matured,
            month_estimates=months,
        )

    total_weight = sum(m["weight"] for m in usable)
    value = sum(m["diff"] * m["weight"] for m in usable) / total_weight
    each_month_weight = sum(m["diff"] for m in usable) / len(usable)

    # Leave-one-grid-out jackknife over every grid that contributes.
    all_grids = sorted(set(treated_grids) | set(control_grids))
    jk_values: list[dict[str, Any]] = []
    jk_ok = True
    for grid in all_grids:
        reduced = [o for o in observations if o.grid != grid]
        reduced_months = [m for m in month_estimates(reduced) if m["estimated"]]
        if not reduced_months:
            jk_ok = False
            jk_values.append({"grid": grid, "value": None, "reason": "去掉该网格后没有可比月份"})
            continue
        reduced_weight = sum(m["weight"] for m in reduced_months)
        if reduced_weight <= 0:
            jk_ok = False
            jk_values.append({"grid": grid, "value": None, "reason": "去掉该网格后权重为零"})
            continue
        jk_value = (
            sum(m["diff"] * m["weight"] for m in reduced_months) / reduced_weight
        )
        jk_values.append({"grid": grid, "value": jk_value})

    naive_low, naive_high = _pooled_naive_interval(observations, confidence)
    ci_low = ci_high = se = None
    df = None
    ci_method = "不可用"
    if jk_ok and len(all_grids) >= 2:
        values = [j["value"] for j in jk_values]
        n = len(values)
        mean_jk = sum(values) / n
        variance = (n - 1) / n * sum((v - mean_jk) ** 2 for v in values)
        se = math.sqrt(max(variance, 0.0))
        df = n - 1
        if se == 0:
            ci_low = ci_high = value
            ci_method = "留一网格刀切法（各网格结果一致，区间宽度为零）"
        else:
            t = student_t_quantile(1 - (1 - confidence) / 2, df)
            ci_low, ci_high = value - t * se, value + t * se
            ci_method = f"留一网格刀切法（t 分布，df={df}，{int(round(confidence*100))}%）"
    if ci_low is None:
        ci_method = "聚类区间不可估计（去掉任一网格后无法保留两侧），仅给出低估不确定性的合并区间"

    notes = []
    if each_month_weight is not None and abs(each_month_weight - value) > 1e-9:
        notes.append(
            f"按月等权的结果为 {each_month_weight*100:.1f} 个百分点，"
            "与按成熟事项加权的主估计不同，说明月份权重会影响数值"
        )
    if not jk_ok:
        notes.append("网格数不足以形成留一网格区间，不能把合并区间当作完整不确定性")
    return Estimate(
        "estimated",
        "已估计（区间是否支持判断见结论）",
        value,
        ci_low,
        ci_high,
        ci_method,
        naive_low,
        naive_high,
        se,
        df,
        [m["month"] for m in usable],
        treated_grids,
        control_grids,
        treated_matured,
        control_matured,
        months,
        notes,
        jk_values,
    )


def _gate_estimate(est: Estimate, protocol: dict[str, Any], maturity: dict[str, Any]) -> Estimate:
    """Apply pre-declared minimum data requirements before any conclusion."""

    if est.status != "estimated":
        return est
    notes = list(est.notes)
    if len(est.treated_grids) < protocol["minimum_treated_grids"]:
        est.status = "insufficient_treated"
        est.status_label = (
            f"实施网格仅 {len(est.treated_grids)} 个，少于预设的 "
            f"{protocol['minimum_treated_grids']} 个，不能下结论"
        )
        est.notes = notes
        return est
    if len(est.months_used) < protocol["minimum_control_months"]:
        est.status = "insufficient_months"
        est.status_label = (
            f"可比月份仅 {len(est.months_used)} 个，少于预设的 "
            f"{protocol['minimum_control_months']} 个，不能下结论"
        )
        est.notes = notes
        return est
    if (
        est.treated_matured < protocol["minimum_matured_per_arm"]
        or est.control_matured < protocol["minimum_matured_per_arm"]
    ):
        est.status = "insufficient_matured"
        est.status_label = (
            "成熟事项不足：实施侧 "
            f"{est.treated_matured} 件、对照侧 {est.control_matured} 件，"
            f"预设每组至少 {protocol['minimum_matured_per_arm']} 件，不能下结论"
        )
        est.notes = notes
        return est
    if maturity.get("end_of_window_incomplete"):
        est.status = "immature_end"
        est.status_label = (
            "比较时段末端仍有较多未成熟事项（成熟比例 "
            f"{maturity['end_matured_share']*100:.0f}%，低于预设 "
            f"{maturity['completion_floor']*100:.0f}%），不能下结论"
        )
        est.notes = notes + ["未成熟事项未计入失败，但期末覆盖不足使结论需要等待成熟后重算"]
        return est
    if est.ci_low is None or est.ci_high is None:
        est.status = "unidentified"
        est.status_label = "聚类不确定性无法估计，不能下结论"
        est.notes = notes
        return est
    width = est.ci_high - est.ci_low
    if width > protocol["max_ci_width"]:
        est.status = "imprecise"
        est.status_label = (
            f"区间宽度 {width*100:.1f} 个百分点，超过预设上限 "
            f"{protocol['max_ci_width']*100:.0f} 个百分点，不能下结论"
        )
        est.notes = notes + ["区间过宽，不能据此决定扩面、调整或继续观察的比例"]
        return est
    if est.ci_low <= 0 <= est.ci_high:
        est.status = "inconclusive"
        est.status_label = "区间跨越 0，方向不能确定，不能下结论"
        est.notes = notes
        return est
    est.status = "supported"
    direction = "高于" if est.value > 0 else "低于"
    est.status_label = f"区间不含 0：实施侧按期办结率{direction}同期对照"
    est.notes = notes
    return est


def _maturity(
    tables: dict[str, list[dict[str, Any]]],
    audit: dict[str, Any],
    protocol: dict[str, Any],
    blocked: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    blocked = blocked or set()
    outcomes = index_by(tables["outcomes"], lambda row: (row["grid"], row["month"]))
    observed = [
        (month, row)
        for (grid, month), row in outcomes.items()
        if month in audit["comparison_months"] and (grid, month) not in blocked
    ]
    received = sum(row["received"] for _, row in observed)
    matured = sum(row["matured"] for _, row in observed)
    timely = sum(row["timely"] for _, row in observed)
    last_month = max((month for month, _ in observed), default=None)
    # the last month of the pre-specified window is where observation delay bites
    window_end = min(protocol["comparison_end"], max(audit["comparison_months"]))
    boundary = [row for month, row in observed if month == window_end]
    if not boundary and last_month:
        boundary = [row for month, row in observed if month == last_month]
        window_end = last_month
    end_received = sum(row["received"] for row in boundary)
    end_matured = sum(row["matured"] for row in boundary)
    floor = protocol.get("mature_completion_floor", 0.7)
    share = (matured / received) if received else None
    end_share = (end_matured / end_received) if end_received else None
    incomplete = bool(
        end_share is not None
        and end_share < floor
        and protocol["data_through"] >= (last_month or "")
    )
    return {
        "received": received,
        "matured": matured,
        "timely": timely,
        "mature_share": share,
        "last_observed_month": last_month,
        "window_end_month": window_end,
        "end_received": end_received,
        "end_matured": end_matured,
        "end_matured_share": end_share,
        "completion_floor": floor,
        "end_of_window_incomplete": incomplete,
        "open_items_at_end": end_received - end_matured,
        "rule": "未成熟事项不计入分子也不计入分母；不按未办结计入失败",
    }


def _scope(
    tables: dict[str, list[dict[str, Any]]],
    audit: dict[str, Any],
    observations: list[ArmObservation],
    protocol: dict[str, Any],
) -> dict[str, Any]:
    treated = [o for o in observations if o.arm == "treated"]
    control = [o for o in observations if o.arm == "control"]
    soon = [
        o
        for o in control
        if o.distance_to_start is not None
        and o.distance_to_start <= design.CONTROL_MONTHS_BEFORE_START_THRESHOLD
    ]
    never = [o for o in control if o.distance_to_start is None]
    treated_pop = sum(
        tables_grid_population(tables, gid) for gid in {o.grid for o in treated}
    )
    total_pop = sum(grid["population"] for grid in tables["grids"])
    area_types = sorted(
        {
            grid["area_type"]
            for grid in tables["grids"]
            if grid["id"] in {o.grid for o in treated}
        }
    )
    shocks = sorted({shock for o in observations for shock in o.shocks})
    return {
        "treated_grids": sorted({o.grid for o in treated}),
        "control_grids": sorted({o.grid for o in control}),
        "months": sorted({o.month for o in observations}),
        "area_types": area_types,
        "treated_population": treated_pop,
        "total_population": total_pop,
        "population_share": treated_pop / total_pop if total_pop else None,
        "control_observations": len(control),
        "soon_to_start_control_observations": len(soon),
        "soon_to_start_share": len(soon) / len(control) if control else None,
        "never_started_control_observations": len(never),
        "never_started_share": len(never) / len(control) if control else None,
        "shock_types_present": shocks,
        "shock_policy": protocol["shock_policy"],
        "start_month_policy": protocol["start_month_policy"],
        "weighting": protocol["weighting"],
        "control_policy": protocol["control_policy"],
        "single_grid_control_months": _single_control_months(control),
    }


def tables_grid_population(tables: dict[str, list[dict[str, Any]]], grid_id: str) -> int:
    for grid in tables["grids"]:
        if grid["id"] == grid_id:
            return grid["population"]
    return 0


def _single_control_months(control: list[ArmObservation]) -> list[str]:
    by_month: dict[str, set[str]] = {}
    for o in control:
        by_month.setdefault(o.month, set()).add(o.grid)
    return sorted(month for month, grids in by_month.items() if len(grids) == 1)


def _run_variant(
    tables: dict[str, list[dict[str, Any]]],
    protocol: dict[str, Any],
    months: list[str],
    name: str,
    *,
    weighting: str | None = None,
    control_source: str | None = None,
    exclude_shock: bool = False,
    exclude_soon: bool = False,
    start_month_policy: str | None = None,
    include_partial: bool | None = None,
    month_equal_weights: bool = False,
) -> dict[str, Any]:
    variant_protocol = dict(protocol)
    if weighting:
        variant_protocol["weighting"] = weighting
    if control_source:
        variant_protocol["control_source"] = control_source
    if start_month_policy:
        variant_protocol["start_month_policy"] = start_month_policy
    if include_partial is not None:
        variant_protocol["partial_exposure_policy"] = (
            "include_as_treated" if include_partial else "exclude"
        )
    blocked = _blocked_cells(tables)
    outcomes = index_by(tables["outcomes"], lambda row: (row["grid"], row["month"]))
    audit = design.audit_design(tables, months, variant_protocol, outcomes, blocked)
    observations = build_observations(
        tables,
        audit,
        variant_protocol,
        weighting=variant_protocol["weighting"],
        control_source=variant_protocol["control_source"],
        exclude_shock=exclude_shock,
        exclude_soon=exclude_soon,
        include_partial=include_partial,
    )
    est = _estimate_from(observations, variant_protocol)
    if est.status == "estimated" and est.ci_low is None:
        est.status = "interval_unavailable"
        est.status_label = "点估计可得，但该设定下无法形成聚类区间"
    if month_equal_weights and est.status == "estimated":
        usable = [m for m in est.month_estimates if m["estimated"]]
        equal = sum(m["diff"] for m in usable) / len(usable)
        est.value = equal
        est.notes = est.notes + ["本期敏感性对所有可比月份等权，主估计按成熟事项加权"]
    scope = _scope(tables, audit, observations, variant_protocol)
    return {
        "name": name,
        "status": est.status,
        "estimate": est.as_dict(),
        "scope": scope,
    }


def _sensitivity(
    tables: dict[str, list[dict[str, Any]]],
    protocol: dict[str, Any],
    months: list[str],
    primary: Estimate,
) -> list[dict[str, Any]]:
    variants = [
        (
            "无加权（每个网格等权，不按人口）",
            dict(weighting="equal"),
        ),
        (
            "只用始终未启动的网格作对照",
            dict(control_source="never_started"),
        ),
        (
            "排除同期冲击涉及的网格月份",
            dict(exclude_shock=True),
        ),
        (
            "排除临近自身启动（<=3 个月）的对照月份",
            dict(exclude_soon=True),
        ),
        (
            "启动当月计入实施",
            dict(start_month_policy="include"),
        ),
        (
            "不足月/暂停月份计入实施（按暴露天数折算）",
            dict(include_partial=True),
        ),
        (
            "每个月等权合并",
            dict(month_equal_weights=True),
        ),
    ]
    results = []
    for name, kwargs in variants:
        result = _run_variant(tables, protocol, months, name, **kwargs)
        result["depends_on_primary_structure"] = _variant_dependence(name, result, primary)
        results.append(result)
    return results


def _variant_dependence(name: str, result: dict[str, Any], primary: Estimate) -> str:
    est = result["estimate"]
    if est["status"] != "estimated":
        return "该设定下无法估计，说明主估计的结构依赖在此设定下不成立"
    if primary.value is None or est["value"] is None:
        return "无法比较"
    diff = abs(est["value"] - primary.value)
    if diff < 0.02:
        return "与主估计接近"
    return f"与主估计相差 {diff*100:.1f} 个百分点，结论对此设定敏感"


def analyze(
    tables: dict[str, list[dict[str, Any]]],
    protocol: dict[str, Any],
    protocol_version: int | None = None,
) -> dict[str, Any]:
    months = display_months(tables, pre_months=1, post_months=1)
    quality = check_sources(tables)
    blocked = {
        (item.get("grid"), item.get("month"))
        for item in quality
        if item["severity"] == "warning" and item.get("grid") and item.get("month")
    }
    outcomes = {
        key: row
        for key, row in index_by(
            tables["outcomes"], lambda row: (row["grid"], row["month"])
        ).items()
        if key not in blocked
    }
    audit = design.audit_design(tables, months, protocol, outcomes, blocked)
    observations = build_observations(
        tables,
        audit,
        protocol,
        weighting=protocol["weighting"],
        control_source=protocol["control_source"],
    )
    maturity = _maturity(tables, audit, protocol, blocked)
    estimate = _estimate_from(observations, protocol)
    estimate = _gate_estimate(estimate, protocol, maturity)
    scope = _scope(tables, audit, observations, protocol)
    sensitivity = _sensitivity(tables, protocol, months, estimate)
    control_objects = design.control_comparison_objects(audit)
    control_changes = _control_changes(control_objects)
    grid_coverage = _grid_coverage(audit, observations, protocol)
    shocks = _shock_notes(tables, audit)
    conclusion = _conclusion(estimate, protocol, maturity, scope, sensitivity, shocks)

    return {
        "report_format": 1,
        "sentinel_version": SENTINEL_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "protocol": protocol,
        "protocol_version": protocol_version,
        "sources_fingerprint": sources_fingerprint(tables),
        "sources": tables,
        "summary": {
            "grids": len(tables["grids"]),
            "outcome_rows": len(tables["outcomes"]),
            "shock_rows": len(tables["shocks"]),
            "comparison_start": protocol["comparison_start"],
            "comparison_end": protocol["comparison_end"],
            "data_through": protocol["data_through"],
        },
        "design": {
            "comparison_months": audit["comparison_months"],
            "months_with_treated": audit["months_with_treated"],
            "months_with_control": audit["months_with_control"],
            "common_months": audit["common_months"],
            "no_control_months": audit["no_control_months"],
            "removed_outside_comparison": audit["removed_outside_comparison"],
            "control_objects": control_objects,
            "control_changes": control_changes,
            "grid_coverage": grid_coverage,
            "rows": audit["rows"],
            "grids_ever_started": audit["grids_ever_started"],
            "control_months": audit["control_months"],
            "treated_months": audit["treated_months"],
            "by_grid": audit["by_grid"],
            "soon_to_start_observations": len(audit["soon_to_start_controls"]),
            "never_started_observations": len(audit["never_started_controls"]),
            "shocks": audit["shocks_in_comparison"],
            "shocks_all": tables["shocks"],
            "shocks_overlapping_treatment": audit["shocks_overlapping_treatment"],
            "season_note": (
                "资料只有月份，没有季节字段；季节与日历月份完全重合，"
                "无法把季节影响与措施影响分开，只能用同月对照减少共同季节影响"
            ),
            "control_rule": (
                "网格从启动当月（按预设规则）起不再作为对照；退出后也不会自动回到对照，"
                "除非重新冻结方案明确改回。对照资格逐月判定并列出。"
            ),
        },
        "maturity": maturity,
        "estimate": estimate.as_dict(),
        "scope": scope,
        "quality": {
            "issues": quality,
            "blocking": [item for item in quality if item["severity"] == "error"],
            "excluded_grid_months": sorted(
                f"{grid} {month}" for grid, month in blocked if grid and month
            ),
            "rule": "计数口径矛盾的行不进入估计，也不被当成失败或零；该网格仍保留在覆盖表中",
        },
        "sensitivity": sensitivity,
        "adjustment": adjusted_estimate(observations, protocol["confidence_level"]),
        "shock_analysis": shocks,
        "conclusion": conclusion,
        "limitations": _limitations(protocol, scope, maturity, shocks),
        "provenance": {
            "protocol_version": protocol_version,
            "frozen_protocol": protocol_version is not None,
            "source_fingerprint": sources_fingerprint(tables),
            "note": "本报告由冻结方案和当前资料重算生成；方案修订或资料变化后旧结论需按影响范围复核",
        },
    }


def _control_changes(control_objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    changes = []
    previous: set[str] | None = None
    previous_month: str | None = None
    for obj in control_objects:
        current = set(obj["controls"])
        if previous is not None and current != previous:
            changes.append(
                {
                    "month": obj["month"],
                    "previous_month": previous_month,
                    "added": sorted(current - previous),
                    "removed": sorted(previous - current),
                    "reason": "对照资格随各网格启动/暂停/退出计划逐月变化",
                }
            )
        previous = current
        previous_month = obj["month"]
    return changes


def _grid_coverage(
    audit: dict[str, Any],
    observations: list[ArmObservation],
    protocol: dict[str, Any],
) -> list[dict[str, Any]]:
    used = {o.grid: set() for o in observations}
    for o in observations:
        used.setdefault(o.grid, set()).add(o.arm)
    rows = []
    for grid in sorted(audit["by_grid"]):
        entry = audit["by_grid"][grid]
        rows.append(
            {
                "grid": grid,
                "area_type": entry.get("area_type", ""),
                "observed_months": entry["observed_months"],
                "received": entry["received"],
                "matured": entry["matured"],
                "control_months": entry["control_months"],
                "treated_months": entry["treated_months"],
                "used_as_control": "control" in used.get(grid, set()),
                "used_as_treated": "treated" in used.get(grid, set()),
                "eligible_control_months": entry["eligible_months"],
                "excluded_months": entry["excluded_months"],
                "first_observed": entry["first_observed"],
                "last_observed": entry["last_observed"],
            }
        )
    return rows


def _shock_notes(
    tables: dict[str, list[dict[str, Any]]], audit: dict[str, Any]
) -> dict[str, Any]:
    events = []
    for shock in tables["shocks"]:
        grid = shock["grid"]
        timeline = audit["timelines"].get(grid)
        start = ""
        if timeline and timeline.rollout:
            start = timeline.rollout.get("start_date", "")
        overlap = "无法区分"
        in_comparison = shock["month"] in audit["comparison_months"]
        events.append(
            {
                "grid": grid,
                "month": shock["month"],
                "type": shock["type"],
                "intensity": shock["intensity"],
                "grid_start": start,
                "in_comparison": in_comparison,
                "overlap": overlap,
                "note": (
                    "该月同时有措施暴露与冲击，二者影响无法区分"
                    if in_comparison
                    else "该月不在比较时段，但可能影响相邻月份的基线"
                ),
            }
        )
    confounded = sorted(
        {
            e["month"]
            for e in events
            if e["in_comparison"]
        }
    )
    return {
        "events": events,
        "months_with_shocks_in_comparison": confounded,
        "statement": (
            "同期冲击与措施在同一网格月份同时发生，资料无法把两者分开；"
            "只能列出并做排除敏感性，不能声称已控制"
            if confounded
            else "比较时段内没有登记的同期冲击，但未登记因素仍可能同时影响结果"
        ),
    }


def _limitations(
    protocol: dict[str, Any],
    scope: dict[str, Any],
    maturity: dict[str, Any],
    shocks: dict[str, Any],
) -> list[str]:
    items = [
        "对照资格逐月变化；结果只适用于所列可比月份与网格，不能外推到启动后或退出后的月份",
        "网格不是随机分配；试点网格在人口、城乡类型或基础服务上可能与对照不同，"
        "估计是同期差异，只有在可比性成立时才可读作措施影响",
        "季节与月份完全重合，同月对照只能抵消共同季节影响，不能区分网格特有的季节性",
        "事项成熟需要观察延迟；未成熟事项未计入失败，但期末成熟不足时不能下结论",
        "相关性不等于已证实因果；本报告不提供网格排名，也不允许按结果删减网格",
    ]
    if scope.get("soon_to_start_share"):
        items.append(
            f"对照观测中有 {scope['soon_to_start_share']*100:.0f}% 来自距自身启动不超过 3 个月的网格，"
            "可能存在启动前趋势；已给出排除这些月份的敏感性"
        )
    if scope.get("single_grid_control_months"):
        items.append(
            "以下月份只有 1 个对照网格，单网格波动会直接进入差异："
            + "、".join(scope["single_grid_control_months"])
        )
    if maturity.get("end_of_window_incomplete"):
        items.append("比较时段末端成熟比例低于预设下限，期末结果尚不成熟")
    if shocks.get("months_with_shocks_in_comparison"):
        items.append(
            "比较时段内存在同期冲击月份："
            + "、".join(shocks["months_with_shocks_in_comparison"])
            + "，措施与冲击无法区分"
        )
    if protocol["control_policy"] == "not_yet_started":
        items.append(
            "已实施网格在后续月份不再作为对照，避免了用启动后的自身变化作反事实，"
            "但对照会随实施推进而减少"
        )
    return items


def _conclusion(
    estimate: Estimate,
    protocol: dict[str, Any],
    maturity: dict[str, Any],
    scope: dict[str, Any],
    sensitivity: list[dict[str, Any]],
    shocks: dict[str, Any],
) -> dict[str, Any]:
    can_conclude = estimate.status == "supported"
    text = estimate.status_label
    recommendation = {
        "supported": "可把结果作为后续扩面或调整的参考，但仍需说明比较对象和适用范围",
        "imprecise": "继续观察，积累更多成熟事项或更多可比月份后再看",
        "inconclusive": "继续观察；方向未定，不应据此扩大或调整",
        "unidentified": "继续观察并补充对照网格或月份；当前不能判断",
        "insufficient_matured": "等待事项成熟后重算，期间不改变口径",
        "insufficient_months": "继续观察，至少覆盖预设的对照月份数",
        "insufficient_treated": "继续观察，等待更多网格进入实施",
        "no_control": "保留尚未实施的网格作为对照，或延后对它们的实施；当前不能估计",
        "no_common_month": "等待出现同时包含实施与合格对照的月份，或重新审视预设比较时段",
        "no_treated": "继续观察，等待实施网格出现可用的成熟结果",
        "no_data": "继续观察并补充资料",
        "immature_end": "等待期末事项成熟后按原方案重算，不提前下结论",
    }.get(estimate.status, "不能下结论")
    if can_conclude:
        sensitive = [
            s["name"]
            for s in sensitivity
            if s["status"] == "estimated"
            and s["estimate"]["value"] is not None
            and estimate.value is not None
            and (s["estimate"]["value"] > 0) != (estimate.value > 0)
        ]
        if sensitive:
            can_conclude = False
            recommendation = (
                "敏感性分析中部分设定下方向与主估计相反，不能据此下确定结论；"
                "需说明结论对设定敏感"
            )
            text = "主估计区间不含 0，但在不同设定下方向会改变，不能视为稳健结论"
    return {
        "can_conclude": can_conclude,
        "status": estimate.status,
        "text": text,
        "recommendation": recommendation,
        "decision_options": {
            "expand": "在可比网格扩大：仅在结论可支持且适用范围明确时考虑",
            "adjust": "调整措施：需先确认具体机制，当前差异本身不说明应调整什么",
            "observe": "继续观察：对照不足、结果未成熟或区间过宽时采用",
        },
        "prohibited": [
            "不能把实施前后的全部变化都归功于措施",
            "不能把已实施后的月份继续当作对照",
            "不能因为某个网格结果不理想就把它从分析中删掉",
            "不能把相关性表述为已证实的因果",
            "不能只给排名而省略不确定性和适用范围",
        ],
        "immature_handling": maturity["rule"],
        "maturity": maturity,
    }


def compare_reports(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Say which parts of an earlier conclusion a change touches.

    Used after a protocol revision or new data: the old report is never deleted
    silently, but its affected components are named so readers know which
    statements no longer stand.
    """

    old_spec = old.get("protocol", {})
    new_spec = new.get("protocol", {})
    spec_changes = [
        {
            "field": field,
            "old": old_spec.get(field),
            "new": new_spec.get(field),
        }
        for field in sorted(set(old_spec) | set(new_spec))
        if old_spec.get(field) != new_spec.get(field)
    ]
    old_estimate = old.get("estimate", {})
    new_estimate = new.get("estimate", {})
    old_months = set(old_estimate.get("months_used", []))
    new_months = set(new_estimate.get("months_used", []))
    old_scope = old.get("scope", {})
    new_scope = new.get("scope", {})
    affected: list[str] = []
    if spec_changes:
        affected.append("结果口径/比较规则改变，全部数值需按新方案重读")
    if old.get("sources_fingerprint") != new.get("sources_fingerprint"):
        affected.append("资料表有增改，涉及新增或修订月份的覆盖与估计")
    if old_scope.get("treated_grids") != new_scope.get("treated_grids"):
        affected.append("实施网格集合变化")
    if old_scope.get("control_grids") != new_scope.get("control_grids"):
        affected.append("对照网格集合变化")
    if old_months != new_months:
        affected.append("纳入估计的月份变化")
    if old_estimate.get("status") != new_estimate.get("status"):
        affected.append("估计状态变化：" + str(old_estimate.get("status_label")) + " → " + str(new_estimate.get("status_label")))
    if old.get("conclusion", {}).get("can_conclude") != new.get("conclusion", {}).get("can_conclude"):
        affected.append("能否下结论的判定变化")
    old_rows = {(r["grid"], r["month"]): r for r in old.get("design", {}).get("rows", [])}
    new_rows = {(r["grid"], r["month"]): r for r in new.get("design", {}).get("rows", [])}
    changed_cells = []
    for key in sorted(set(old_rows) | set(new_rows)):
        before = old_rows.get(key)
        after = new_rows.get(key)
        if before is None or after is None:
            changed_cells.append({"grid": key[0], "month": key[1], "change": "新增" if before is None else "移除"})
            continue
        if before.get("status") != after.get("status"):
            changed_cells.append(
                {
                    "grid": key[0],
                    "month": key[1],
                    "change": f"{before.get('status_label')} → {after.get('status_label')}",
                }
            )
    return {
        "protocol_changes": spec_changes,
        "affected_parts": affected or ["结构未变，仅按新资料重算的数值可能小幅变动"],
        "changed_cells": changed_cells[:200],
        "old": {
            "id": old.get("id"),
            "protocol_version": old.get("protocol_version"),
            "value_pct": old_estimate.get("value_pct"),
            "ci_low_pct": old_estimate.get("ci_low_pct"),
            "ci_high_pct": old_estimate.get("ci_high_pct"),
            "status_label": old_estimate.get("status_label"),
        },
        "new": {
            "id": new.get("id"),
            "protocol_version": new.get("protocol_version"),
            "value_pct": new_estimate.get("value_pct"),
            "ci_low_pct": new_estimate.get("ci_low_pct"),
            "ci_high_pct": new_estimate.get("ci_high_pct"),
            "status_label": new_estimate.get("status_label"),
        },
    }
