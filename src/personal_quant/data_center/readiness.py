from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from personal_quant.data_center.positions import load_positions


@dataclass(frozen=True)
class ReadinessResult:
    positions_path: Path
    ready: bool
    position_count: int
    errors: list[str]
    focus_positions: bool = True


def check_inputs(positions_path: Path, focus_positions: bool = True) -> ReadinessResult:
    if not focus_positions:
        return ReadinessResult(positions_path, True, 0, [], focus_positions=False)
    errors: list[str] = []
    position_count = 0
    if not positions_path.exists():
        errors.append("POSITIONS_FILE_MISSING")
        return ReadinessResult(positions_path, False, position_count, errors, focus_positions=True)
    if positions_path.name.lower() == "positions.example.csv":
        errors.append("POSITIONS_FILE_IS_EXAMPLE")
    try:
        positions = load_positions(positions_path)
        position_count = len(positions)
    except Exception as exc:
        errors.append(f"POSITIONS_FILE_INVALID: {exc}")
    if _contains_example_marker(positions_path):
        errors.append("POSITIONS_FILE_IS_EXAMPLE")
    errors = sorted(set(errors))
    return ReadinessResult(positions_path, len(errors) == 0, position_count, errors, focus_positions=True)


def render_readiness_report(result: ReadinessResult) -> str:
    rows = [
        "# 输入就绪检查",
        "",
        "## 输入就绪检查",
        f"- 持仓文件：{result.positions_path}",
        f"- 持仓关注：{'是' if result.focus_positions else '否，暂不关注持仓个股'}",
        f"- 持仓数量：{result.position_count}",
        f"- 结果：{'可以进入严格完整输出' if result.ready else '不能进入严格完整输出'}",
        "",
        "## 缺口",
    ]
    if result.errors:
        rows.extend([f"- {error}" for error in result.errors])
    else:
        rows.append("- 暂无")
    if not result.focus_positions:
        rows.extend(
            [
                "",
                "## 当前范围",
                "暂不关注持仓个股，因此不检查持仓文件，也不生成逐股持仓建议。",
            ]
        )
        return "\n".join(rows) + "\n"
    rows.extend(
        [
            "",
            "## 持仓文件要求",
            "| 项目 | 内容 |",
            "|---|---|",
            "| 推荐路径 | data/positions.csv |",
            "| 必填字段 | code、name、quantity、cost_price |",
            "| 可选字段 | buy_date、available_quantity、position_value、note |",
            "| 示例文件 | data/positions.example.csv 只能参考，不能直接用于正式报告 |",
            "",
            "## 修复动作",
            "| 缺口 | 动作 |",
            "|---|---|",
            "| POSITIONS_FILE_MISSING | 创建或更新真实持仓文件 data/positions.csv |",
            "| POSITIONS_FILE_IS_EXAMPLE | 复制示例文件后替换为真实持仓，并移除示例标记 |",
            "| POSITIONS_FILE_INVALID | 按必填字段和数值规则修正 CSV |",
            "",
            "## 验收命令",
            "| 目的 | 命令 |",
            "|---|---|",
            "| 检查输入 | python -m personal_quant.cli check-inputs --as-of 2026-07-09 --positions data/positions.csv --output-dir reports |",
            "| 严格生成 | python -m personal_quant.cli daily-pack --as-of 2026-07-09 --positions data/positions.csv --output-dir reports --split --require-positions |",
        ]
    )
    return "\n".join(rows) + "\n"


def _contains_example_marker(path: Path) -> bool:
    content = path.read_text(encoding="utf-8-sig")
    return "示例持仓请复制后替换" in content
