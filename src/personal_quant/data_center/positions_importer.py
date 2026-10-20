from __future__ import annotations

import csv
from pathlib import Path

from personal_quant.data_center.positions import load_positions


STANDARD_COLUMNS = (
    "code",
    "name",
    "quantity",
    "cost_price",
    "available_quantity",
    "position_value",
    "note",
)

COLUMN_ALIASES = {
    "code": ("code", "证券代码", "股票代码", "代码"),
    "name": ("name", "证券名称", "股票名称", "名称"),
    "quantity": ("quantity", "持仓数量", "股票余额", "证券数量", "当前持仓"),
    "cost_price": ("cost_price", "成本价", "持仓成本", "成本价格", "参考成本价", "买入均价"),
    "available_quantity": ("available_quantity", "可用数量", "可卖数量", "可用余额"),
    "position_value": ("position_value", "持仓市值", "市值", "最新市值"),
    "note": ("note", "备注"),
}

REQUIRED_SOURCE_FIELDS = ("code", "name", "quantity", "cost_price")
NUMERIC_FIELDS = {"quantity", "cost_price", "available_quantity", "position_value"}


def import_positions_csv(source: Path, output: Path, encoding: str = "utf-8-sig") -> Path:
    if not source.exists():
        raise FileNotFoundError(f"导入源文件不存在: {source}")
    with source.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        mapping = _build_column_mapping(reader.fieldnames or [])
        rows = [_normalize_row(row, mapping) for row in reader]
    if not rows:
        raise ValueError("导入源文件为空")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=STANDARD_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    load_positions(output)
    return output


def import_latest_positions_export(
    source_dir: Path,
    output: Path,
    pattern: str = "*.csv",
    encoding: str = "utf-8-sig",
) -> Path:
    if not source_dir.exists():
        raise FileNotFoundError(f"导出目录不存在: {source_dir}")
    candidates = sorted(
        [path for path in source_dir.glob(pattern) if path.is_file()],
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(f"导出目录没有匹配文件: {source_dir / pattern}")
    source = candidates[0]
    import_positions_csv(source, output, encoding=encoding)
    return source


def _build_column_mapping(fieldnames: list[str]) -> dict[str, str]:
    normalized = {field.strip(): field for field in fieldnames}
    mapping: dict[str, str] = {}
    for standard, aliases in COLUMN_ALIASES.items():
        matched = next((normalized[alias] for alias in aliases if alias in normalized), None)
        if matched is not None:
            mapping[standard] = matched
    missing = [field for field in REQUIRED_SOURCE_FIELDS if field not in mapping]
    if missing:
        raise ValueError(f"缺少持仓字段: {', '.join(missing)}")
    return mapping


def _normalize_row(row: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for column in STANDARD_COLUMNS:
        source_column = mapping.get(column)
        value = (row.get(source_column) or "").strip() if source_column else ""
        normalized[column] = _normalize_numeric(value) if column in NUMERIC_FIELDS else value
    return normalized


def _normalize_numeric(value: str) -> str:
    return value.replace(",", "").strip()
