from __future__ import annotations

from personal_quant.market_analysis.classifier import BoardView, MarketSummary


def build_summary_report(summary: MarketSummary) -> str:
    mainline = [item for item in summary.board_views if item.label == "主升候选"][:10]
    rebound = [item for item in summary.board_views if item.label == "反弹候选"][:10]
    cashout = [item for item in summary.board_views if item.label == "高位兑现"][:10]
    one_day = [item for item in summary.board_views if item.label == "一日游风险"][:10]
    hot_boards = sorted(summary.board_views, key=lambda item: item.board.change_pct, reverse=True)[:10]

    rows: list[str] = []
    rows.append(f"# 本地盘面信息总结（{summary.dataset.as_of.isoformat()}）")
    rows.append("")
    rows.append("## 数据质量")
    rows.append(f"- 板块数量：{len(summary.dataset.boards)}")
    rows.append(f"- 外围指标数量：{len(summary.dataset.externals)}")
    rows.append("- 数据原则：关键数据为空则失败，不用假数据补齐")
    rows.append("")
    rows.append("## 市场状态")
    rows.append(f"- 状态：{summary.market_state}")
    rows.append(f"- 今日操作建议：{summary.operation_advice}")
    rows.append("")
    rows.append("## 当天热门板块")
    rows.extend(board_table(hot_boards))
    rows.append("")
    rows.append("## 主升候选")
    if mainline:
        rows.extend(board_table(mainline))
    else:
        rows.append("暂无满足主升条件的建仓候选。")
    rows.append("")
    rows.append("## 反弹候选")
    rows.extend(board_table(rebound) if rebound else ["暂无。"])
    rows.append("")
    rows.append("## 高位兑现")
    rows.extend(board_table(cashout) if cashout else ["暂无。"])
    rows.append("")
    rows.append("## 一日游风险")
    rows.extend(board_table(one_day) if one_day else ["暂无。"])
    rows.append("")
    rows.append("## 外围市场")
    rows.extend(external_table(summary))
    rows.append("")
    rows.append("## 后续待补")
    rows.append("- 持仓建议、新闻影响、晚间复盘将在后续阶段接入。")
    return "\n".join(rows) + "\n"


def board_table(items: list[BoardView]) -> list[str]:
    rows = [
        "| 板块 | 类型 | 今日涨幅 | 5日涨幅 | 20日涨幅 | 60日涨幅 | 成交变化 | 上涨家数 | 主力净流入 | 标签 | 原因 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for item in items:
        board = item.board
        rows.append(
            f"| {board.name} | {board.board_type} | {board.change_pct:.2f}% | "
            f"{board.five_day_return:.2f}% | {board.twenty_day_return:.2f}% | {board.sixty_day_return:.2f}% | "
            f"{board.volume_ratio:.2f} | {board.up_count} | {board.main_net_inflow / 100000000:.2f}亿 | "
            f"{item.label} | {item.reason} |"
        )
    return rows


def external_table(summary: MarketSummary) -> list[str]:
    rows = [
        "| 外围指标 | 5-6月涨幅 | 7月涨幅 |",
        "|---|---:|---:|",
    ]
    for item in summary.dataset.externals:
        rows.append(f"| {item.name} | {item.may_june_return:.2f}% | {item.july_return:.2f}% |")
    return rows
