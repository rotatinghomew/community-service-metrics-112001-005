"""Pre-specified analysis protocol: freeze, revision and diff.

The protocol is the only place that decides the outcome measure, the comparison
window and what may count as a control.  Revisions are append-only and need a
stated reason, so a measure cannot be swapped after seeing an unfavourable grid.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .domain import (
    SourceValidationError,
    display_months,
    parse_month,
    sources_fingerprint,
    validate_sources,
)

PROTOCOL_FIELDS: dict[str, Any] = {
    "title": "措施效果评估",
    "estimand": "实施网格与同期尚未实施网格之间按期办结率的差异（百分点）",
    "outcome_measure": "timely_rate",
    "outcome_definition": "按期办结数 / 已成熟事项数；未成熟事项既不进分子也不进分母",
    "start_month_policy": "exclude",
    "control_policy": "not_yet_started",
    "control_source": "any_not_yet_started",
    "maturation_policy": "matured_units_only",
    "partial_exposure_policy": "exclude",
    "paused_policy": "exclude",
    "shock_policy": "flag",
    "weighting": "population",
    "confidence_level": 0.9,
    "comparison_start": "",
    "comparison_end": "",
    "data_through": "",
    "minimum_matured_per_arm": 20,
    "minimum_control_months": 1,
    "minimum_treated_grids": 1,
    "max_ci_width": 0.3,
    "mature_completion_floor": 0.7,
    "require_common_months": True,
    "notes": "",
}

ENUMS: dict[str, tuple[str, ...]] = {
    "outcome_measure": ("timely_rate",),
    "start_month_policy": ("exclude", "include"),
    "control_policy": ("not_yet_started",),
    "control_source": ("any_not_yet_started", "other_grids_not_yet_started", "never_started"),
    "maturation_policy": ("matured_units_only", "fully_matured_months_only"),
    "partial_exposure_policy": ("exclude", "include_as_treated"),
    "paused_policy": ("exclude", "count_as_unexposed"),
    "shock_policy": ("flag", "exclude"),
    "weighting": ("population", "equal"),
}

FIELD_LABELS = {
    "title": "方案名称",
    "estimand": "估计目标",
    "outcome_measure": "结果口径",
    "outcome_definition": "口径定义",
    "start_month_policy": "启动当月处理",
    "control_policy": "对照资格规则",
    "control_source": "对照来源",
    "maturation_policy": "成熟度处理",
    "partial_exposure_policy": "不足月实施处理",
    "paused_policy": "暂停期处理",
    "shock_policy": "同期冲击处理",
    "weighting": "合并权重",
    "confidence_level": "置信水平",
    "comparison_start": "比较时段开始",
    "comparison_end": "比较时段结束",
    "data_through": "资料截至月份",
    "minimum_matured_per_arm": "每组最少成熟事项",
    "minimum_control_months": "最少对照月份",
    "minimum_treated_grids": "最少实施网格数",
    "max_ci_width": "最大可接受区间宽度",
    "mature_completion_floor": "期末成熟下限",
    "require_common_months": "要求同月可比",
    "notes": "预设说明",
}


class ProtocolError(ValueError):
    pass


def allowed_fields() -> set[str]:
    return set(PROTOCOL_FIELDS)


def draft_protocol(tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """A draft populated from the current sources; nothing is frozen yet."""

    months = display_months(tables, pre_months=0, post_months=0)
    draft = dict(PROTOCOL_FIELDS)
    draft["comparison_start"] = months[0] if months else ""
    draft["comparison_end"] = months[-1] if months else ""
    draft["data_through"] = months[-1] if months else ""
    return draft


def validate_protocol(spec: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    if not isinstance(spec, dict):
        raise ProtocolError("方案应为对象")
    unknown = set(spec) - allowed_fields()
    if unknown:
        issues.append(
            "方案含未允许字段（不能加入按结果挑选网格或口径的开关）："
            + "、".join(sorted(unknown))
        )
    missing = allowed_fields() - set(spec)
    if missing:
        issues.append("方案缺少字段：" + "、".join(sorted(missing)))
    clean: dict[str, Any] = {}
    for field, default in PROTOCOL_FIELDS.items():
        value = spec.get(field, default)
        if field in ENUMS and value not in ENUMS[field]:
            issues.append(f"{FIELD_LABELS[field]}应为 {ENUMS[field]} 之一")
        if field == "title":
            if not isinstance(value, str) or not value.strip():
                issues.append("方案名称不能为空")
            else:
                clean[field] = value.strip()
        elif field in ("estimand", "outcome_definition", "notes"):
            if not isinstance(value, str):
                issues.append(f"{FIELD_LABELS[field]}应为文本")
            else:
                clean[field] = value.strip()
        elif field in ("comparison_start", "comparison_end", "data_through"):
            if not isinstance(value, str):
                issues.append(f"{FIELD_LABELS[field]}应为 YYYY-MM")
            else:
                try:
                    parse_month(value)
                    clean[field] = value
                except ValueError:
                    issues.append(f"{FIELD_LABELS[field]}应为 YYYY-MM：{value!r}")
        elif field == "confidence_level":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.5 < value < 1
            ):
                issues.append("置信水平应在 0.5 与 1 之间")
            else:
                clean[field] = float(value)
        elif field == "max_ci_width":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < value <= 1
            ):
                issues.append("最大可接受区间宽度应在 0 与 1 之间")
            else:
                clean[field] = float(value)
        elif field == "mature_completion_floor":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.1 <= value <= 1
            ):
                issues.append("期末成熟下限应在 0.1 与 1 之间")
            else:
                clean[field] = float(value)
        elif field == "require_common_months":
            if not isinstance(value, bool):
                issues.append("要求同月可比应为是/否")
            else:
                clean[field] = value
        elif field in ("minimum_matured_per_arm", "minimum_treated_grids"):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                issues.append(f"{FIELD_LABELS[field]}应为非负整数")
            else:
                clean[field] = value
        elif field == "minimum_control_months":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                issues.append("最少对照月份应为非负整数")
            else:
                clean[field] = value
        else:
            clean[field] = value
    if not issues:
        if clean["comparison_start"] > clean["comparison_end"]:
            issues.append("比较时段开始晚于结束")
        if clean["data_through"] < clean["comparison_end"]:
            issues.append("资料截至月份早于比较时段结束，不能覆盖所声明的比较时段")
        if clean["control_source"] == "never_started" and clean["control_policy"] != "not_yet_started":
            issues.append("对照规则不一致")
    if issues:
        raise ProtocolError("；".join(issues))
    return clean


def diff_protocol(old: dict[str, Any] | None, new: dict[str, Any]) -> list[dict[str, Any]]:
    if old is None:
        return []
    changes = []
    for field in PROTOCOL_FIELDS:
        if old.get(field) != new.get(field):
            changes.append(
                {
                    "field": field,
                    "label": FIELD_LABELS.get(field, field),
                    "old": old.get(field),
                    "new": new.get(field),
                }
            )
    return changes


class StateStore:
    """JSON file store for frozen protocols and saved reports."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _empty(self) -> dict[str, Any]:
        return {"protocols": [], "reports": []}

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"状态文件无法读取：{exc}") from exc
        if not isinstance(data, dict):
            raise ProtocolError("状态文件结构无效")
        data.setdefault("protocols", [])
        data.setdefault("reports", [])
        return data

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def active_protocol(self, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
        data = data if data is not None else self.load()
        active = [record for record in data["protocols"] if record.get("active")]
        return active[-1] if active else None

    def freeze(
        self,
        spec: dict[str, Any],
        reason: str,
        tables: dict[str, list[dict[str, Any]]],
    ) -> dict[str, Any]:
        validate_sources(tables)
        clean = validate_protocol(spec)
        if not isinstance(reason, str) or not reason.strip():
            raise ProtocolError("冻结或修订方案必须说明理由")
        data = self.load()
        previous = self.active_protocol(data)
        if previous and diff_protocol(previous["spec"], clean) == []:
            raise ProtocolError("方案内容与当前生效版本相同，无需修订")
        if previous is None and reason.strip() in ("", "初次冻结"):
            reason = reason.strip() or "初次冻结"
        version = (max((p["version"] for p in data["protocols"]), default=0)) + 1
        changes = diff_protocol(previous["spec"] if previous else None, clean)
        record = {
            "version": version,
            "spec": clean,
            "reason": reason.strip(),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "source_fingerprint": sources_fingerprint(tables),
            "changes_from_previous": changes,
            "active": True,
            "supersedes": previous["version"] if previous else None,
        }
        for old in data["protocols"]:
            if old.get("active"):
                old["active"] = False
                old["superseded_by"] = version
                old["superseded_at"] = record["created_at"]
        data["protocols"].append(record)
        self.save(data)
        return record

    def protocol_version(self, version: int) -> dict[str, Any]:
        data = self.load()
        for record in data["protocols"]:
            if record["version"] == version:
                return record
        raise ProtocolError(f"找不到方案版本 {version}")

    def save_report(self, report: dict[str, Any]) -> dict[str, Any]:
        data = self.load()
        report_id = f"R{len(data['reports']) + 1:04d}"
        report = copy.deepcopy(report)
        report["id"] = report_id
        report["created_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        data["reports"].append(report)
        self.save(data)
        return report

    def reports(self) -> list[dict[str, Any]]:
        return self.load()["reports"]

    def report(self, report_id: str) -> dict[str, Any]:
        for report in self.reports():
            if report.get("id") == report_id:
                return report
        raise ProtocolError(f"找不到报告 {report_id}")
