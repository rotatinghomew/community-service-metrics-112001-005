"""Design audit: who may serve as a comparison, when, and what cannot be separated.

Treatment is never "after the start date, therefore treated".  Each grid-month
gets an exposure status derived from the plan; a grid stops qualifying as a
control from the month its exposure begins and only returns if the protocol
says an exit period may re-enter (it never silently re-enters).  Meanwhile every
control month carries its distance to its own future start, so the analysis can
show how much of the comparison rests on soon-to-start grids.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from .domain import (
    Exposure,
    compute_exposure,
    index_by,
    month_end,
    month_span,
    month_start,
    outcome_key,
    parse_date,
    shift_month,
)

CONTROL_MONTHS_BEFORE_START_THRESHOLD = 3


@dataclass
class GridTimeline:
    grid_id: str
    population: int
    area_type: str
    rollout: dict[str, Any] | None
    start_date: dt.date | None
    exposure: dict[str, Exposure] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "grid": self.grid_id,
            "population": self.population,
            "area_type": self.area_type,
            "start_date": self.rollout.get("start_date") if self.rollout else "",
            "intensity": self.rollout.get("intensity") if self.rollout else "none",
            "paused_from": self.rollout.get("paused_from") if self.rollout else "",
            "paused_to": self.rollout.get("paused_to") if self.rollout else "",
            "end_date": self.rollout.get("end_date") if self.rollout else "",
            "months": {month: exp.as_dict() for month, exp in self.exposure.items()},
        }


def build_timelines(
    tables: dict[str, list[dict[str, Any]]], months: list[str], start_month_policy: str
) -> dict[str, GridTimeline]:
    rollouts = index_by(tables["rollouts"], lambda row: row["grid"])
    timelines: dict[str, GridTimeline] = {}
    for grid in tables["grids"]:
        rollout = rollouts.get(grid["id"])
        start = None
        if rollout is not None:
            try:
                start = parse_date(rollout.get("start_date", ""))
            except ValueError:
                start = None
        timeline = GridTimeline(
            grid_id=grid["id"],
            population=grid["population"],
            area_type=grid["area_type"],
            rollout=rollout,
            start_date=start,
        )
        for month in months:
            timeline.exposure[month] = compute_exposure(month, rollout, start_month_policy)
        timelines[grid["id"]] = timeline
    return timelines


def month_range(months: list[str], start: str, end: str) -> list[str]:
    lo, hi = max(start, months[0]), min(end, months[-1])
    return [m for m in months if lo <= m <= hi]


def control_gap_months(timeline: GridTimeline, month: str) -> int | None:
    """Months between this observation and the grid's own start for controls."""

    if timeline.start_date is None:
        return None
    start_month = f"{timeline.start_date.year:04d}-{timeline.start_date.month:02d}"
    if month >= start_month:
        return 0
    gap = 0
    cursor = month
    while shift_month(cursor, 1) < start_month:
        cursor = shift_month(cursor, 1)
        gap += 1
    return gap + 1


def months_until_start(timeline: GridTimeline, month: str) -> int | None:
    """Strict number of calendar months from ``month`` to the start month."""

    if timeline.start_date is None:
        return None
    start_month = f"{timeline.start_date.year:04d}-{timeline.start_date.month:02d}"
    if start_month < month:
        return 0
    count = 0
    cursor = month
    while cursor < start_month:
        cursor = shift_month(cursor, 1)
        count += 1
    return count


def treated_statuses(protocol: dict[str, Any]) -> set[str]:
    if protocol["partial_exposure_policy"] == "include_as_treated":
        return {"treated", "partial"}
    return {"treated"}


def classify_grid_month(
    timeline: GridTimeline,
    month: str,
    protocol: dict[str, Any],
) -> dict[str, Any]:
    exposure = timeline.exposure[month]
    status = exposure.status
    if status == "transition" and protocol["partial_exposure_policy"] == "include_as_treated":
        status = "treated"
    if status in ("partial", "paused"):
        if protocol["partial_exposure_policy"] == "include_as_treated":
            status = "treated"
        elif protocol["paused_policy"] == "count_as_unexposed" and status == "paused":
            status = "control"
    return {
        "status": status,
        "raw_status": exposure.status,
        "exposed_days": exposure.exposed_days,
        "active_days": exposure.active_days,
        "paused_days": exposure.paused_days,
        "intensity": exposure.intensity,
        "reasons": list(exposure.reasons),
    }


def control_eligibility(
    timeline: GridTimeline,
    month: str,
    protocol: dict[str, Any],
    has_treated_this_month: bool,
) -> tuple[bool, list[str]]:
    """Whether a grid-month can enter the comparison arm."""

    reasons: list[str] = []
    classification = classify_grid_month(timeline, month, protocol)
    status = classification["status"]
    if status in ("treated", "partial"):
        return False, ["该网格措施已开始实施，此后不再作为对照"]
    if status == "post_exit":
        return False, ["该网格已退出措施，退出期不能视作未实施对照"]
    if status == "paused":
        if protocol["paused_policy"] == "count_as_unexposed":
            return True, ["当月暂停，按方案计为未暴露"]
        return False, ["当月暂停，按方案排除"]
    if timeline.start_date is None:
        return True, ["网格无启动计划，保持对照"]
    start_month = f"{timeline.start_date.year:04d}-{timeline.start_date.month:02d}"
    if month == start_month and protocol["start_month_policy"] == "include":
        return False, ["启动当月按方案计入实施"]
    if month >= start_month:
        return False, ["启动后不再是对照"]
    if protocol["control_source"] == "never_started":
        return False, ["方案限定只用始终未启动的网格作对照，该网格有启动计划"]
    if protocol["control_source"] == "other_grids_not_yet_started":
        source_status = classify_grid_month(timeline, month, protocol)
        if source_status["status"] != "control":
            return False, ["该网格当月不是对照"]
    if protocol["require_common_months"] and not has_treated_this_month:
        return False, ["方案要求同月可比，该月没有实施网格"]
    gap = months_until_start(timeline, month)
    if gap is not None and gap <= CONTROL_MONTHS_BEFORE_START_THRESHOLD:
        reasons.append(f"距该网格自身启动仅 {gap} 个月，可能存在启动前趋势")
    reasons.append("尚在启动日期之前")
    return True, reasons


def grid_timeline_rows(
    timelines: dict[str, GridTimeline],
    months: list[str],
    protocol: dict[str, Any],
    outcomes: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """One row per grid-month for the coverage audit table."""

    treated_months: set[str] = set()
    treated_set = treated_statuses(protocol)
    for month in months:
        if any(
            classify_grid_month(timeline, month, protocol)["status"] in treated_set
            for timeline in timelines.values()
        ):
            treated_months.add(month)
    rows = []
    for grid in sorted(timelines):
        timeline = timelines[grid]
        for month in months:
            classification = classify_grid_month(timeline, month, protocol)
            eligible, eligibility_reasons = control_eligibility(
                timeline, month, protocol, month in treated_months
            )
            record = outcomes.get((grid, month))
            rows.append(
                {
                    "grid": grid,
                    "month": month,
                    "population": timeline.population,
                    "area_type": timeline.area_type,
                    "status": classification["status"],
                    "raw_status": classification["raw_status"],
                    "status_label": _status_label(classification["status"]),
                    "control_eligible": eligible,
                    "eligibility_reasons": eligibility_reasons,
                    "exposed_days": classification["exposed_days"],
                    "active_days": classification["active_days"],
                    "paused_days": classification["paused_days"],
                    "intensity": classification["intensity"],
                    "received": record.get("received") if record else None,
                    "matured": record.get("matured") if record else None,
                    "timely": record.get("timely") if record else None,
                    "observed": record is not None,
                }
            )
    return rows


def _status_label(status: str) -> str:
    return {
        "control": "对照（尚未启动）",
        "transition": "启动当月（过渡，方案排除）",
        "treated": "已实施",
        "partial": "不足月/部分实施",
        "paused": "暂停",
        "post_exit": "退出后（不再作对照）",
    }.get(status, status)


def shock_exposure(
    tables: dict[str, list[dict[str, Any]]],
    months: list[str],
    protocol: dict[str, Any],
) -> dict[str, Any]:
    shocks = tables["shocks"]
    by_grid: dict[str, list[dict[str, Any]]] = {}
    for shock in shocks:
        by_grid.setdefault(shock["grid"], []).append(shock)
    return {
        "policy": protocol["shock_policy"],
        "events": shocks,
        "by_grid": by_grid,
        "confounded_months": sorted({shock["month"] for shock in shocks}),
    }


def audit_design(
    tables: dict[str, list[dict[str, Any]]],
    months: list[str],
    protocol: dict[str, Any],
    outcomes: dict[tuple[str, str], dict[str, Any]],
    blocked: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    blocked = blocked or set()
    timelines = build_timelines(tables, months, protocol["start_month_policy"])
    rows = grid_timeline_rows(timelines, months, protocol, outcomes)
    comparison = month_range(months, protocol["comparison_start"], protocol["comparison_end"])

    control_months = [
        row
        for row in rows
        if row["month"] in comparison and row["control_eligible"] and row["observed"]
    ]
    treated_months = [
        row
        for row in rows
        if row["month"] in comparison
        and row["status"] in treated_statuses(protocol)
        and row["observed"]
    ]

    months_with_treated = sorted({row["month"] for row in treated_months})
    months_with_control = sorted({row["month"] for row in control_months})
    common_months = sorted(set(months_with_treated) & set(months_with_control))

    by_grid: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = by_grid.setdefault(
            row["grid"],
            {
                "grid": row["grid"],
                "control_months": 0,
                "treated_months": 0,
                "observed_months": 0,
                "matured": 0,
                "received": 0,
                "first_observed": None,
                "last_observed": None,
                "eligible_months": [],
                "excluded_months": [],
            },
        )
        if row["month"] not in comparison:
            continue
        if row["observed"]:
            entry["observed_months"] += 1
            entry["matured"] += row["matured"] or 0
            entry["received"] += row["received"] or 0
            if entry["first_observed"] is None:
                entry["first_observed"] = row["month"]
            entry["last_observed"] = row["month"]
        if row["control_eligible"]:
            entry["control_months"] += 1
            entry["eligible_months"].append(row["month"])
        elif row["status"] in treated_statuses(protocol):
            entry["treated_months"] += 1
        else:
            entry["excluded_months"].append(
                {"month": row["month"], "status": row["status"], "label": row["status_label"]}
            )

    soon_to_start = [
        row
        for row in control_months
        if months_until_start(timelines[row["grid"]], row["month"]) is not None
        and months_until_start(timelines[row["grid"]], row["month"])
        <= CONTROL_MONTHS_BEFORE_START_THRESHOLD
    ]
    never_started_months = [
        row for row in control_months if timelines[row["grid"]].start_date is None
    ]
    grids_ever_started = [gid for gid, t in timelines.items() if t.start_date is not None]

    overlapping = []
    for shock in tables["shocks"]:
        grid = shock["grid"]
        affected_months = month_span(shock["month"], shock["month"])
        treated_here = [
            row
            for row in treated_months
            if row["grid"] == grid and row["month"] in affected_months
        ]
        if treated_here:
            overlapping.append(
                {
                    "grid": grid,
                    "month": shock["month"],
                    "type": shock["type"],
                    "intensity": shock["intensity"],
                    "affects": "该网格已实施月份",
                }
            )
    all_months_with_shock = sorted({s["month"] for s in tables["shocks"]})
    shocks_in_comparison = [
        s for s in tables["shocks"] if s["month"] in comparison
    ]

    no_control_months = [m for m in comparison if m in months_with_treated and m not in months_with_control]
    return {
        "months": months,
        "comparison_months": comparison,
        "removed_outside_comparison": [m for m in months if m not in comparison],
        "timelines": timelines,
        "rows": rows,
        "by_grid": by_grid,
        "control_months": control_months,
        "treated_months": treated_months,
        "months_with_treated": months_with_treated,
        "months_with_control": months_with_control,
        "common_months": common_months,
        "no_control_months": no_control_months,
        "soon_to_start_controls": soon_to_start,
        "never_started_controls": never_started_months,
        "grids_ever_started": grids_ever_started,
        "shocks": {s["grid"] + "|" + s["month"] + "|" + s["type"]: s for s in tables["shocks"]},
        "shocks_in_comparison": shocks_in_comparison,
        "shocks_overlapping_treatment": overlapping,
        "shock_policy": protocol["shock_policy"],
        "treated_statuses": sorted(treated_statuses(protocol)),
        "comparison_start": protocol["comparison_start"],
        "comparison_end": protocol["comparison_end"],
        "data_through": protocol["data_through"],
    }


def control_comparison_objects(audit: dict[str, Any]) -> list[dict[str, Any]]:
    """Narrative rows: which grids can be compared against each treated month."""

    rows = []
    treated_by_month: dict[str, list[str]] = {}
    for row in audit["treated_months"]:
        treated_by_month.setdefault(row["month"], []).append(row["grid"])
    for month in audit["comparison_months"]:
        treated = sorted(treated_by_month.get(month, []))
        controls = sorted(row["grid"] for row in audit["control_months"] if row["month"] == month)
        soon = sorted(
            row["grid"]
            for row in audit["control_months"]
            if row["month"] == month
            and months_until_start(audit["timelines"][row["grid"]], month) is not None
            and months_until_start(audit["timelines"][row["grid"]], month)
            <= CONTROL_MONTHS_BEFORE_START_THRESHOLD
        )
        notes = []
        if not treated:
            notes.append("当月没有可用的实施观测")
        if not controls:
            notes.append("当月已无合格对照")
        if soon:
            notes.append("部分对照网格临近自身启动")
        if any(
            shock["month"] == month and shock["grid"] in controls
            for shock in audit["shocks_in_comparison"]
        ):
            notes.append("当月对照网格有同期冲击，措施与冲击无法区分")
        rows.append(
            {
                "month": month,
                "treated": treated,
                "controls": controls,
                "soon_controls": soon,
                "notes": notes,
                "value": bool(treated) and bool(controls),
            }
        )
    return rows
