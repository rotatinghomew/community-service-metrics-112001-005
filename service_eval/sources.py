"""Loading and appending to the source tables.

The service accepts edited copies of the four tables.  Files are read as UTF-8,
validated before use, and never mutated on disk.  Appending an outcome or a
shock writes a complete replacement table after validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .domain import (
    OUTCOME_FIELDS,
    SHOCK_FIELDS,
    SOURCE_NAMES,
    SourceValidationError,
    check_sources,
    validate_sources,
)


class SourceError(ValueError):
    pass


class SourceBundle:
    def __init__(self, tables: dict[str, list[dict[str, Any]]], origin: str = ""):
        self.tables = tables
        self.origin = origin

    def check(self) -> list[dict[str, str]]:
        return check_sources(self.tables)

    def fingerprints(self) -> dict[str, int]:
        return {name: len(self.tables.get(name, [])) for name in SOURCE_NAMES}

    def empty_months_note(self) -> str:
        return (
            "空字符串固定解析为字符串，不解析为数字、否或终态；"
            "缺失月份表示没有观测，不是零"
        )


def tables_from_payload(payload: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise SourceError("上传资料应为对象，包含 grids、rollouts、outcomes、shocks 四个数组")
    extra = set(payload) - set(SOURCE_NAMES)
    if extra:
        raise SourceError("上传资料含未知表：" + "、".join(sorted(extra)))
    tables = {}
    for name in SOURCE_NAMES:
        if name not in payload:
            raise SourceError(f"上传资料缺少 {name}")
        tables[name] = payload[name]
    validate_sources(tables)
    return tables


def load_directory(directory: str | Path) -> SourceBundle:
    directory = Path(directory)
    tables: dict[str, list[dict[str, Any]]] = {}
    for name in SOURCE_NAMES:
        path = directory / f"{name}.json"
        if not path.exists():
            raise SourceError(f"找不到 {path}")
        try:
            tables[name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceError(f"{path.name} 无法读取：{exc}") from exc
    validate_sources(tables)
    return SourceBundle(tables, origin=str(directory))


def write_table(directory: str | Path, name: str, rows: list[dict[str, Any]]) -> None:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_outcome(
    directory: str | Path, tables: dict[str, list[dict[str, Any]]], row: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    clean = _clean_numeric_row(row, OUTCOME_FIELDS, "结果记录")
    updated = {name: list(rows) for name, rows in tables.items()}
    key = (clean["grid"], clean["month"])
    if any((r["grid"], r["month"]) == key for r in updated["outcomes"]):
        raise SourceError(f"网格 {clean['grid']} 的 {clean['month']} 已有结果记录")
    updated["outcomes"].append(clean)
    updated["outcomes"].sort(key=lambda r: (r["grid"], r["month"]))
    try:
        validate_sources(updated)
    except SourceValidationError as exc:
        raise SourceError("；".join(exc.issues)) from exc
    write_table(directory, "outcomes", updated["outcomes"])
    return updated


def append_shock(
    directory: str | Path, tables: dict[str, list[dict[str, Any]]], row: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(row.get("type"), str) or not row["type"].strip():
        raise SourceError("冲击类型不能为空")
    clean = _clean_numeric_row(row, SHOCK_FIELDS, "冲击记录", numeric=())
    clean["type"] = clean["type"].strip()
    if clean["intensity"] not in ("low", "medium", "high"):
        raise SourceError("冲击强度应为 low、medium 或 high")
    updated = {name: list(rows) for name, rows in tables.items()}
    updated["shocks"].append(clean)
    updated["shocks"].sort(key=lambda r: (r["grid"], r["month"], r["type"]))
    try:
        validate_sources(updated)
    except SourceValidationError as exc:
        raise SourceError("；".join(exc.issues)) from exc
    write_table(directory, "shocks", updated["shocks"])
    return updated


def _clean_numeric_row(
    row: dict[str, Any], fields: tuple[str, ...], label: str, numeric: tuple[str, ...] = ("received", "timely", "matured")
) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise SourceError(f"{label}应为对象")
    extra = set(row) - set(fields)
    if extra:
        raise SourceError(f"{label}含未知字段：" + "、".join(sorted(extra)))
    missing = set(fields) - set(row)
    if missing:
        raise SourceError(f"{label}缺少字段：" + "、".join(sorted(missing)))
    clean: dict[str, Any] = {}
    for field in fields:
        value = row[field]
        if field in numeric:
            if isinstance(value, bool):
                raise SourceError(f"{field} 应为整数")
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise SourceError(f"{field} 应为整数") from None
            if number < 0:
                raise SourceError(f"{field} 不能为负数")
            clean[field] = number
        else:
            if not isinstance(value, str):
                raise SourceError(f"{field} 应为文本")
            clean[field] = value.strip()
    if not clean.get("grid"):
        raise SourceError("网格不能为空")
    if not clean.get("month"):
        raise SourceError("月份不能为空")
    return clean
