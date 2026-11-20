"""Source parsing, validation and exposure timelines.

The source tables are deliberately treated as sparse: a missing month is a
missing observation, never a zero.  Empty strings mean "the source does not
state this fact", which is different from ``no`` or ``zero``.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable

SOURCE_NAMES = ("grids", "rollouts", "outcomes", "shocks")

GRID_FIELDS = ("id", "population", "area_type")
ROLLOUT_FIELDS = ("grid", "start_date", "intensity", "paused_from", "paused_to")
ROLLOUT_OPTIONAL_FIELDS = ("end_date", "intensity_segments")
OUTCOME_FIELDS = ("grid", "month", "received", "timely", "matured")
SHOCK_FIELDS = ("grid", "month", "type", "intensity")

INTENSITIES = ("none", "low", "medium", "high")
STATUS_LABELS = {
    "control": "对照（尚未启动）",
    "transition": "启动当月（过渡）",
    "treated": "已实施",
    "partial": "部分实施/暂停或退出当月",
    "paused": "暂停",
    "post_exit": "退出后",
}


class SourceValidationError(ValueError):
    """Raised with a list of human readable issues."""

    def __init__(self, issues: Iterable[str]):
        self.issues = list(issues)
        super().__init__("；".join(self.issues) if self.issues else "资料校验失败")


def parse_month(value: str) -> tuple[int, int]:
    try:
        parsed = dt.date.fromisoformat(value + "-01")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"月份格式应为 YYYY-MM：{value!r}") from exc
    return parsed.year, parsed.month


def parse_date(value: str) -> dt.date | None:
    if value == "":
        return None
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{value!r}") from exc


def month_start(ym: str) -> dt.date:
    year, month = parse_month(ym)
    return dt.date(year, month, 1)


def month_end(ym: str) -> dt.date:
    year, month = parse_month(ym)
    return dt.date(year, month, calendar.monthrange(year, month)[1])


def shift_month(ym: str, delta: int) -> str:
    year, month = parse_month(ym)
    index = year * 12 + (month - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_of(value: dt.date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def month_span(first: str, last: str) -> list[str]:
    if first > last:
        return []
    out: list[str] = []
    current = first
    while current <= last:
        out.append(current)
        current = shift_month(current, 1)
    return out


def overlap_days(
    a_start: dt.date, a_end: dt.date, b_start: dt.date | None, b_end: dt.date | None
) -> int:
    if b_start is None or b_end is None:
        return 0
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    return max(0, (end - start).days + 1)


def _int(value: Any, label: str, issues: list[str]) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(f"{label} 应为整数")
        return 0
    if value < 0:
        issues.append(f"{label} 不能为负数")
    return value


def _nonempty_str(value: Any, label: str, issues: list[str]) -> str:
    if not isinstance(value, str) or value.strip() == "":
        issues.append(f"{label} 不能为空")
        return ""
    return value.strip()


def check_sources(tables: dict[str, list[dict[str, Any]]]) -> list[dict[str, str]]:
    """Return every issue found.

    ``error`` issues make the source unusable for analysis; ``warning`` issues
    are internally inconsistent counts.  The affected grid-month is then left
    out of estimation and listed in the report, rather than silently repaired or
    treated as a failure.
    """

    return _check_sources(tables, strict=False)


def validate_sources(tables: dict[str, list[dict[str, Any]]]) -> None:
    """Validate all four tables.  Unknown fields are rejected to catch typos."""

    errors = [item for item in _check_sources(tables, strict=True) if item["severity"] == "error"]
    if errors:
        raise SourceValidationError(item["message"] for item in errors)


def _check_sources(
    tables: dict[str, list[dict[str, Any]]], strict: bool
) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []

    def issue(message: str, severity: str = "error", **context: Any) -> None:
        found.append({"severity": severity, "message": message, **context})

    issues: list[str] = []
    for name in SOURCE_NAMES:
        if name not in tables:
            issues.append(f"缺少资料表 {name}.json")
            continue
        if not isinstance(tables[name], list):
            issues.append(f"{name}.json 应为记录数组")
            continue
        if not all(isinstance(row, dict) for row in tables[name]):
            issues.append(f"{name}.json 每条记录应为对象")
    if issues:
        if strict:
            raise SourceValidationError(issues)
        return [{"severity": "error", "message": text} for text in issues]

    grids = tables["grids"]
    grid_ids: set[str] = set()
    for index, row in enumerate(grids):
        label = f"grids[{index}]"
        unknown = set(row) - set(GRID_FIELDS)
        if unknown:
            issues.append(f"{label} 含未知字段：{', '.join(sorted(unknown))}")
        missing = set(GRID_FIELDS) - set(row)
        if missing:
            issues.append(f"{label} 缺少字段：{', '.join(sorted(missing))}")
            continue
        grid_id = _nonempty_str(row["id"], f"{label}.id", issues)
        if grid_id in grid_ids:
            issues.append(f"网格编号重复：{grid_id}")
        grid_ids.add(grid_id)
        _int(row["population"], f"{label}.population", issues)
        _nonempty_str(row["area_type"], f"{label}.area_type", issues)

    rollouts = tables["rollouts"]
    rollout_grids: set[str] = set()
    for index, row in enumerate(rollouts):
        label = f"rollouts[{index}]"
        unknown = set(row) - set(ROLLOUT_FIELDS) - set(ROLLOUT_OPTIONAL_FIELDS)
        if unknown:
            issues.append(f"{label} 含未知字段：{', '.join(sorted(unknown))}")
        missing = set(ROLLOUT_FIELDS) - set(row)
        if missing:
            issues.append(f"{label} 缺少字段：{', '.join(sorted(missing))}")
            continue
        grid_id = _nonempty_str(row["grid"], f"{label}.grid", issues)
        if grid_id and grid_id not in grid_ids:
            issues.append(f"{label}.grid 指向不存在的网格：{grid_id}")
        if grid_id in rollout_grids:
            issues.append(f"rollouts 中网格重复：{grid_id}")
        rollout_grids.add(grid_id)

        intensity = row["intensity"]
        if intensity not in INTENSITIES:
            issues.append(f"{label}.intensity 应为 {INTENSITIES} 之一")
        try:
            start = parse_date(row["start_date"])
            pause_from = parse_date(row["paused_from"])
            pause_to = parse_date(row["paused_to"])
            end = parse_date(row.get("end_date", ""))
        except ValueError as exc:
            issues.append(f"{label}：{exc}")
            continue
        if (pause_from is None) != (pause_to is None):
            issues.append(f"{label} 暂停起止日期必须同时为空或同时填写")
        elif pause_from and pause_to and pause_to < pause_from:
            issues.append(f"{label} 暂停结束早于开始")
        if start is None:
            if intensity != "none":
                issues.append(f"{label} 尚未启动时强度应为 none（空日期不等于零）")
            if pause_from or pause_to or end:
                issues.append(f"{label} 尚未启动却填写了暂停或退出日期")
        else:
            if pause_from and pause_from < start:
                issues.append(f"{label} 暂停开始早于启动日期")
            if end and end < start:
                issues.append(f"{label} 退出日期早于启动日期")
        segments = row.get("intensity_segments") or []
        if segments:
            if not isinstance(segments, list):
                issues.append(f"{label}.intensity_segments 应为数组")
            else:
                previous_end: dt.date | None = None
                for seg_index, segment in enumerate(segments):
                    seg_label = f"{label}.intensity_segments[{seg_index}]"
                    if not isinstance(segment, dict) or set(segment) != {"from", "to", "intensity"}:
                        issues.append(f"{seg_label} 应仅含 from、to、intensity")
                        continue
                    try:
                        seg_from = parse_date(segment["from"])
                        seg_to = parse_date(segment["to"])
                    except ValueError as exc:
                        issues.append(f"{seg_label}：{exc}")
                        continue
                    if seg_from is None or seg_to is None or seg_to < seg_from:
                        issues.append(f"{seg_label} 起止日期无效")
                    elif start and seg_from < start:
                        issues.append(f"{seg_label} 早于启动日期")
                    elif previous_end and seg_from <= previous_end:
                        issues.append(f"{seg_label} 与前一段重叠或未排序")
                    if segment["intensity"] not in INTENSITIES:
                        issues.append(f"{seg_label}.intensity 无效")
                    previous_end = seg_to

    outcomes = tables["outcomes"]
    outcome_keys: set[tuple[str, str]] = set()
    for index, row in enumerate(outcomes):
        label = f"outcomes[{index}]"
        unknown = set(row) - set(OUTCOME_FIELDS)
        if unknown:
            issues.append(f"{label} 含未知字段：{', '.join(sorted(unknown))}")
        missing = set(OUTCOME_FIELDS) - set(row)
        if missing:
            issues.append(f"{label} 缺少字段：{', '.join(sorted(missing))}")
            continue
        grid_id = _nonempty_str(row["grid"], f"{label}.grid", issues)
        if grid_id and grid_id not in grid_ids:
            issues.append(f"{label}.grid 指向不存在的网格：{grid_id}")
        try:
            parse_month(row["month"])
        except ValueError as exc:
            issues.append(f"{label}：{exc}")
        key = (grid_id, str(row["month"]))
        if key in outcome_keys:
            issues.append(f"outcomes 中网格+月份重复：{grid_id} {row['month']}")
        outcome_keys.add(key)
        received = _int(row["received"], f"{label}.received", issues)
        matured = _int(row["matured"], f"{label}.matured", issues)
        timely = _int(row["timely"], f"{label}.timely", issues)
        if matured > received:
            issue(
                f"{label} 已成熟数大于受理数，无法确认口径",
                "warning",
                grid=grid_id,
                month=str(row["month"]),
            )
        elif timely > matured:
            issue(
                f"{label} 按期数大于已成熟数，无法确认分母口径，未纳入估计",
                "warning",
                grid=grid_id,
                month=str(row["month"]),
            )

    shocks = tables["shocks"]
    for index, row in enumerate(shocks):
        label = f"shocks[{index}]"
        unknown = set(row) - set(SHOCK_FIELDS)
        if unknown:
            issues.append(f"{label} 含未知字段：{', '.join(sorted(unknown))}")
        missing = set(SHOCK_FIELDS) - set(row)
        if missing:
            issues.append(f"{label} 缺少字段：{', '.join(sorted(missing))}")
            continue
        grid_id = _nonempty_str(row["grid"], f"{label}.grid", issues)
        if grid_id and grid_id not in grid_ids:
            issues.append(f"{label}.grid 指向不存在的网格：{grid_id}")
        try:
            parse_month(row["month"])
        except ValueError as exc:
            issues.append(f"{label}：{exc}")
        _nonempty_str(row["type"], f"{label}.type", issues)
        if row["intensity"] not in ("low", "medium", "high"):
            issues.append(f"{label}.intensity 应为 low、medium 或 high")

    for text in issues:
        issue(text, "error")
    return found


@dataclass(frozen=True)
class Exposure:
    status: str
    status_label: str
    active_days: int
    exposed_days: int
    paused_days: int
    days_in_month: int
    intensity: str
    intensity_days: dict[str, int]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "status_label": self.status_label,
            "active_days": self.active_days,
            "exposed_days": self.exposed_days,
            "paused_days": self.paused_days,
            "days_in_month": self.days_in_month,
            "intensity": self.intensity,
            "intensity_days": self.intensity_days,
            "reasons": list(self.reasons),
        }


def _segment_intensity_days(
    rollout: dict[str, Any], start: dt.date, end: dt.date
) -> dict[str, int]:
    segments = rollout.get("intensity_segments") or []
    counts: dict[str, int] = {}
    if not segments:
        counts[rollout["intensity"]] = (end - start).days + 1
        return counts
    for segment in segments:
        seg_from = parse_date(segment["from"])
        seg_to = parse_date(segment["to"])
        if seg_from is None or seg_to is None:
            continue
        days = overlap_days(start, end, seg_from, seg_to)
        if days:
            counts[segment["intensity"]] = counts.get(segment["intensity"], 0) + days
    return counts


def compute_exposure(
    month: str,
    rollout: dict[str, Any] | None,
    start_month_policy: str = "exclude",
) -> Exposure:
    """Classify a grid-month relative to its rollout plan.

    ``start_month_policy`` is pre-specified in the protocol and decides whether
    the calendar month containing the start date can be counted as fully
    treated.  The default excludes it as a transition month.
    """

    first = month_start(month)
    last = month_end(month)
    total_days = (last - first).days + 1
    if rollout is None:
        return Exposure(
            "control", STATUS_LABELS["control"], 0, 0, 0, total_days, "none", {},
            ("资料未给出启动计划，按尚未启动处理",),
        )
    start = parse_date(rollout.get("start_date", ""))
    end = parse_date(rollout.get("end_date", ""))
    pause_from = parse_date(rollout.get("paused_from", ""))
    pause_to = parse_date(rollout.get("paused_to", ""))
    if start is None:
        return Exposure(
            "control", STATUS_LABELS["control"], 0, 0, 0, total_days, "none", {},
            ("尚未启动",),
        )
    if last < start:
        return Exposure(
            "control", STATUS_LABELS["control"], 0, 0, 0, total_days, "none", {},
            ("处于启动日期之前",),
        )
    if end is not None and first > end:
        return Exposure(
            "post_exit", STATUS_LABELS["post_exit"], 0, 0, 0, total_days,
            "none", {}, ("退出日期之后",),
        )
    effective_end = end or dt.date.max
    exposed_start = max(first, start)
    exposed_end = min(last, effective_end)
    exposed_days = max(0, (exposed_end - exposed_start).days + 1)
    pauses = overlap_days(first, last, pause_from, pause_to) if pause_from and pause_to else 0
    active_days = max(0, exposed_days - pauses)
    intensity_days = (
        _segment_intensity_days(rollout, exposed_start, exposed_end) if exposed_days else {}
    )
    intensity = (
        next(iter(intensity_days))
        if len(intensity_days) == 1
        else ("mixed" if len(intensity_days) > 1 else "none")
    )
    reasons: list[str] = []
    is_start_month = first <= start <= last
    if is_start_month and start_month_policy == "exclude":
        status = "transition"
        reasons.append("启动当月按方案排除，避免不足月的实施暴露混入")
    elif is_start_month and start.day > 1:
        status = "transition"
        reasons.append("启动当月暴露不足整月")
    elif exposed_days == 0:
        status = "control"
        reasons.append("当月无实施暴露")
    elif active_days == 0:
        status = "paused"
        reasons.append("当月全部处于暂停")
    elif end is not None and first <= end <= last:
        status = "partial"
        reasons.append("退出发生在当月，暴露不足整月")
    elif active_days < total_days:
        status = "partial"
        if pauses:
            reasons.append(f"当月暂停 {pauses} 天，暴露不足整月")
        else:
            reasons.append("当月暴露不足整月")
    else:
        status = "treated"
    if pauses and status in ("treated", "partial"):
        reasons.append(f"暂停记录 {pause_from} 至 {pause_to}")
    return Exposure(
        status,
        STATUS_LABELS[status],
        active_days,
        exposed_days,
        pauses,
        total_days,
        intensity,
        intensity_days,
        tuple(reasons),
    )


def index_by(rows: Iterable[dict[str, Any]], key) -> dict[Any, dict[str, Any]]:
    return {key(row): row for row in rows}


def outcome_key(row: dict[str, Any]) -> tuple[str, str]:
    return (row["grid"], row["month"])


def shock_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (row["grid"], row["month"], row["type"])


def sources_fingerprint(tables: dict[str, list[dict[str, Any]]]) -> str:
    import hashlib
    import json

    payload = json.dumps(tables, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def display_months(
    tables: dict[str, list[dict[str, Any]]], pre_months: int, post_months: int
) -> list[str]:
    months: list[str] = [row["month"] for row in tables["outcomes"]]
    months.extend(row["month"] for row in tables["shocks"])
    for row in tables["rollouts"]:
        for field in ("start_date", "paused_from", "paused_to", "end_date"):
            value = row.get(field) or ""
            if value:
                months.append(value[:7])
    if not months:
        today = dt.date.today()
        months = [f"{today.year:04d}-{today.month:02d}"]
    low = shift_month(min(months), -max(pre_months, 1))
    high = shift_month(max(months), max(post_months, 1))
    return month_span(low, high)
