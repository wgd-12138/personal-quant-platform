from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


REQUIRED_COLUMNS = {"code", "name", "quantity", "cost_price"}
EXAMPLE_MARKER = "示例持仓请复制后替换"


@dataclass(frozen=True)
class PositionSnapshot:
    code: str
    name: str
    quantity: int
    cost_price: float
    buy_date: str | None = None
    available_quantity: int | None = None
    position_value: float | None = None
    note: str | None = None


def load_positions(path: Path) -> list[PositionSnapshot]:
    if not path.exists():
        raise FileNotFoundError(f"持仓文件不存在: {path}")
    content = path.read_text(encoding="utf-8-sig")
    if path.name.lower() == "positions.example.csv" or EXAMPLE_MARKER in content:
        raise ValueError("POSITIONS_FILE_IS_EXAMPLE: 不能使用示例持仓文件生成正式报告")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(f"持仓文件缺少字段: {', '.join(sorted(missing))}")
        positions = [_parse_position(row) for row in reader]
    if not positions:
        raise ValueError("持仓文件为空")
    return positions


def _parse_position(row: dict[str, str]) -> PositionSnapshot:
    code = (row.get("code") or "").strip()
    name = (row.get("name") or "").strip()
    quantity = _parse_int(row.get("quantity"), "quantity", row)
    cost_price = _parse_float(row.get("cost_price"), "cost_price", row)
    if not code or not name or quantity <= 0 or cost_price <= 0:
        raise ValueError(f"持仓行非法: {row}")
    return PositionSnapshot(
        code=code,
        name=name,
        quantity=quantity,
        cost_price=cost_price,
        buy_date=(row.get("buy_date") or "").strip() or None,
        available_quantity=_parse_optional_int(row.get("available_quantity"), "available_quantity", row),
        position_value=_parse_optional_float(row.get("position_value"), "position_value", row),
        note=(row.get("note") or "").strip() or None,
    )


def _parse_int(value: str | None, field: str, row: dict[str, str]) -> int:
    try:
        return int((value or "").strip())
    except ValueError as exc:
        raise ValueError(f"持仓字段 {field} 非法: {row}") from exc


def _parse_float(value: str | None, field: str, row: dict[str, str]) -> float:
    try:
        return float((value or "").strip())
    except ValueError as exc:
        raise ValueError(f"持仓字段 {field} 非法: {row}") from exc


def _parse_optional_int(value: str | None, field: str, row: dict[str, str]) -> int | None:
    if value is None or value.strip() == "":
        return None
    parsed = _parse_int(value, field, row)
    if parsed < 0:
        raise ValueError(f"持仓字段 {field} 不能为负: {row}")
    return parsed


def _parse_optional_float(value: str | None, field: str, row: dict[str, str]) -> float | None:
    if value is None or value.strip() == "":
        return None
    parsed = _parse_float(value, field, row)
    if parsed < 0:
        raise ValueError(f"持仓字段 {field} 不能为负: {row}")
    return parsed
