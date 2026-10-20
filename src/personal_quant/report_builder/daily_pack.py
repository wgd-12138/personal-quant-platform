from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from personal_quant.data_center.models import NewsItem, StockSnapshot
from personal_quant.data_center.positions import PositionSnapshot
from personal_quant.market_analysis.classifier import BoardView, MarketSummary
from personal_quant.news_ai.matcher import match_news
from personal_quant.position_advisor.advisor import PositionAdvice, advise_position
from personal_quant.report_builder.push_contract import build_push_contract_report
from personal_quant.report_builder.push_preview import build_push_preview_report
from personal_quant.report_builder.summary import board_table, external_table
from personal_quant.strategy_signals import render_morning_build_report, render_overnight_arbitrage_report
from personal_quant.watchlist_engine.selector import select_watchlist


REQUIRED_SECTIONS = [
    "数据质量",
    "市场状态",
    "主线判断",
    "当天热门板块",
    "外围市场",
    "消息驱动分析",
    "早盘走势推演",
    "早盘建仓推荐",
    "尾盘隔夜套利",
    "今日操作建议",
    "持仓操作建议",
    "建仓候选",
    "利好利空信息",
    "晚间复盘",
    "次日操作计划",
    "风险提示",
]

POSITIVE_NEWS_KEYWORDS = (
    "利好",
    "政策",
    "支持",
    "订单",
    "增长",
    "中标",
    "扩产",
    "涨价",
    "预增",
    "并购",
    "突破",
    "获批",
    "注册证书",
    "同比增长",
)
NEGATIVE_NEWS_KEYWORDS = ("利空", "监管", "问询", "减持", "处罚", "立案", "下修", "亏损", "承压", "资金出逃")


def build_daily_pack_report(
    summary: MarketSummary,
    positions: list[PositionSnapshot] | None,
    news_items: list[NewsItem] | None,
    focus_positions: bool = True,
) -> str:
    return build_daily_report_sections(summary, positions, news_items, focus_positions=focus_positions)["daily_pack"]


def build_daily_report_sections(
    summary: MarketSummary,
    positions: list[PositionSnapshot] | None,
    news_items: list[NewsItem] | None,
    focus_positions: bool = True,
) -> dict[str, str]:
    positions = positions or []
    news_items = news_items or summary.dataset.news_items
    hot_boards = sorted(summary.board_views, key=lambda item: item.board.change_pct, reverse=True)[:10]
    mainline = [item for item in summary.board_views if item.label == "主升候选"][:10]
    rebound = [item for item in summary.board_views if item.label == "反弹候选"][:10]
    cashout = [item for item in summary.board_views if item.label == "高位兑现"][:10]
    one_day = [item for item in summary.board_views if item.label == "一日游风险"][:10]
    watchlist_candidates = select_watchlist(summary, mainline)
    strict_ready = len(positions) > 0 if focus_positions else True
    readiness_errors = [] if strict_ready else ["POSITIONS_FILE_MISSING"]

    intraday_rows = [f"# 盘中快照（{summary.dataset.as_of.isoformat()}）", ""]
    intraday_rows.extend(_quality_section(summary, positions, news_items, strict_ready, readiness_errors))
    intraday_rows.extend(_market_section(summary, mainline, rebound, cashout, one_day))
    intraday_rows.extend(_mainline_judgement_section(mainline, rebound, cashout, one_day))
    intraday_rows.extend(_fund_strength_section(summary))
    intraday_rows.append("## 当天热门板块")
    intraday_rows.extend(board_table(hot_boards))
    intraday_rows.append("")
    intraday_rows.append("## 外围市场")
    intraday_rows.extend(external_table(summary))
    intraday_rows.append("")
    intraday_rows.extend(_external_resonance_section(summary, mainline, cashout, one_day))
    intraday_rows.extend(_news_impact_section(summary, news_items))
    intraday_rows.extend(_morning_scenario_section(summary, news_items, mainline, rebound, cashout, one_day))
    intraday_rows.extend(_operation_section(summary, mainline, rebound, cashout, one_day))

    positions_rows = [f"# 持仓操作建议（{summary.dataset.as_of.isoformat()}）", ""]
    positions_rows.extend(_position_section(summary, positions, news_items, focus_positions))

    watchlist_rows = [f"# 建仓候选（{summary.dataset.as_of.isoformat()}）", ""]
    watchlist_rows.extend(_watchlist_section(watchlist_candidates, mainline))

    news_rows = [f"# 利好利空信息（{summary.dataset.as_of.isoformat()}）", ""]
    news_rows.extend(_news_section(positions, mainline, news_items, [stock for stock, _ in watchlist_candidates]))

    morning_build_rows = [f"# 早盘建仓推荐（{summary.dataset.as_of.isoformat()}）", ""]
    morning_build_rows.append(render_morning_build_report(summary, news_items, auctions=None).strip())

    overnight_rows = [f"# 尾盘隔夜套利（{summary.dataset.as_of.isoformat()}）", ""]
    overnight_rows.append(render_overnight_arbitrage_report(summary, news_items).strip())

    evening_rows = [f"# 晚间复盘与次日计划（{summary.dataset.as_of.isoformat()}）", ""]
    evening_rows.extend(_evening_section(summary, mainline, rebound, cashout, one_day))
    evening_rows.extend(_next_day_section(summary, mainline, cashout, one_day, watchlist_candidates))
    evening_rows.extend(_morning_scenario_section(summary, news_items, mainline, rebound, cashout, one_day))
    evening_rows.extend(_position_next_day_section(summary, positions, focus_positions))
    evening_rows.extend(_risk_section(positions, mainline, cashout, one_day, watchlist_candidates, focus_positions))

    brief_sections = _build_brief_sections(
        summary,
        positions,
        news_items,
        hot_boards,
        mainline,
        rebound,
        cashout,
        one_day,
        watchlist_candidates,
        strict_ready,
        readiness_errors,
        focus_positions,
    )
    daily_rows = _build_personal_daily_rows(
        summary,
        positions,
        news_items,
        hot_boards,
        mainline,
        rebound,
        cashout,
        one_day,
        watchlist_candidates,
        strict_ready,
        readiness_errors,
        focus_positions,
    )

    sections = {
        "daily_pack": _join_rows(daily_rows),
        "intraday": _join_rows(intraday_rows),
        "positions": _join_rows(positions_rows),
        "watchlist": _join_rows(watchlist_rows),
        "news": _join_rows(news_rows),
        "morning_build": _join_rows(morning_build_rows),
        "overnight_arbitrage": _join_rows(overnight_rows),
        "evening": _join_rows(evening_rows),
    }
    sections["push_preview"] = build_push_preview_report(
        summary.dataset.as_of,
        sections,
        strict_ready=strict_ready,
        readiness_errors=readiness_errors,
        focus_positions=focus_positions,
        brief_sections=brief_sections,
    )
    sections["push_contract"] = build_push_contract_report(
        summary.dataset.as_of,
        sections,
        strict_ready=strict_ready,
        readiness_errors=readiness_errors,
        focus_positions=focus_positions,
    )
    return sections


def _build_personal_daily_rows(
    summary: MarketSummary,
    positions: list[PositionSnapshot],
    news_items: list[NewsItem],
    hot_boards: list[BoardView],
    mainline: list[BoardView],
    rebound: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
    watchlist_candidates: list[tuple[StockSnapshot, BoardView]],
    strict_ready: bool,
    readiness_errors: list[str],
    focus_positions: bool,
) -> list[str]:
    rows = [f"# 每日交易简报（{summary.dataset.as_of.isoformat()}）", ""]
    rows.extend(_brief_conclusion_section(summary, mainline, cashout, one_day, watchlist_candidates))
    rows.extend(_personal_quality_section(summary, positions, news_items, strict_ready, readiness_errors))
    rows.extend(_personal_market_section(summary))
    rows.extend(_personal_mainline_section(mainline, rebound, cashout, one_day))
    rows.extend(_personal_hot_boards_section(hot_boards))
    rows.extend(_brief_external_section(summary, mainline, cashout, one_day))
    rows.extend(_news_impact_section(summary, news_items))
    rows.extend(_morning_scenario_section(summary, news_items, mainline, rebound, cashout, one_day))
    rows.extend(_personal_operation_section(summary, mainline, rebound, cashout))
    rows.extend(_brief_position_section(summary, positions, news_items, focus_positions))
    rows.extend(_brief_watchlist_section(watchlist_candidates, mainline))
    rows.extend(_brief_news_section(positions, mainline, news_items, [stock for stock, _ in watchlist_candidates]))
    rows.append(render_morning_build_report(summary, news_items, auctions=None).strip())
    rows.append(render_overnight_arbitrage_report(summary, news_items).strip())
    rows.append("")
    rows.extend(_personal_evening_section(summary, mainline, rebound, cashout, one_day))
    rows.extend(_personal_next_day_section(summary, mainline, cashout, one_day, watchlist_candidates))
    rows.extend(_personal_risk_section(positions, mainline, cashout, one_day, watchlist_candidates, focus_positions))
    return rows


def _build_brief_sections(
    summary: MarketSummary,
    positions: list[PositionSnapshot],
    news_items: list[NewsItem],
    hot_boards: list[BoardView],
    mainline: list[BoardView],
    rebound: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
    watchlist_candidates: list[tuple[StockSnapshot, BoardView]],
    strict_ready: bool,
    readiness_errors: list[str],
    focus_positions: bool,
) -> dict[str, str]:
    intraday_rows = [f"# 盘中简报（{summary.dataset.as_of.isoformat()}）", ""]
    intraday_rows.extend(_personal_quality_section(summary, positions, news_items, strict_ready, readiness_errors))
    intraday_rows.extend(_personal_market_section(summary))
    intraday_rows.extend(_personal_mainline_section(mainline, rebound, cashout, one_day))
    intraday_rows.extend(_personal_hot_boards_section(hot_boards))
    intraday_rows.extend(_brief_external_section(summary, mainline, cashout, one_day))
    intraday_rows.extend(_news_impact_section(summary, news_items))
    intraday_rows.extend(_morning_scenario_section(summary, news_items, mainline, rebound, cashout, one_day))
    intraday_rows.extend(_personal_operation_section(summary, mainline, rebound, cashout))

    positions_rows = [f"# 持仓简报（{summary.dataset.as_of.isoformat()}）", ""]
    positions_rows.extend(_brief_position_section(summary, positions, news_items, focus_positions))

    watchlist_rows = [f"# 建仓简报（{summary.dataset.as_of.isoformat()}）", ""]
    watchlist_rows.extend(_brief_watchlist_section(watchlist_candidates, mainline))

    news_rows = [f"# 新闻简报（{summary.dataset.as_of.isoformat()}）", ""]
    news_rows.extend(_brief_news_section(positions, mainline, news_items, [stock for stock, _ in watchlist_candidates]))

    morning_build_rows = [f"# 早盘建仓简报（{summary.dataset.as_of.isoformat()}）", ""]
    morning_build_rows.append(render_morning_build_report(summary, news_items, auctions=None).strip())

    overnight_rows = [f"# 尾盘隔夜套利简报（{summary.dataset.as_of.isoformat()}）", ""]
    overnight_rows.append(render_overnight_arbitrage_report(summary, news_items).strip())

    evening_rows = [f"# 晚间简报（{summary.dataset.as_of.isoformat()}）", ""]
    evening_rows.extend(_personal_evening_section(summary, mainline, rebound, cashout, one_day))
    evening_rows.extend(_personal_next_day_section(summary, mainline, cashout, one_day, watchlist_candidates))
    evening_rows.extend(_morning_scenario_section(summary, news_items, mainline, rebound, cashout, one_day))
    evening_rows.extend(_personal_risk_section(positions, mainline, cashout, one_day, watchlist_candidates, focus_positions))

    return {
        "intraday": _join_rows(intraday_rows),
        "positions": _join_rows(positions_rows),
        "watchlist": _join_rows(watchlist_rows),
        "news": _join_rows(news_rows),
        "morning_build": _join_rows(morning_build_rows),
        "overnight_arbitrage": _join_rows(overnight_rows),
        "evening": _join_rows(evening_rows),
    }


def _brief_conclusion_section(
    summary: MarketSummary,
    mainline: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
    watchlist_candidates: list[tuple[StockSnapshot, BoardView]],
) -> list[str]:
    return [
        "## 今日结论",
        f"- 市场：{summary.market_state}",
        f"- 操作：{summary.operation_advice}",
        f"- 主线：{_board_names(mainline) if mainline else '暂无'}",
        f"- 建仓：{_key_stock_text(watchlist_candidates)}",
        f"- 回避：{_board_names((cashout + one_day)[:5])}",
        "",
    ]


def _join_rows(rows: list[str]) -> str:
    return "\n".join(rows).rstrip() + "\n"


def _personal_quality_section(
    summary: MarketSummary,
    positions: list[PositionSnapshot],
    news_items: list[NewsItem],
    strict_ready: bool,
    readiness_errors: list[str],
) -> list[str]:
    data_time = summary.dataset.fetched_at.strftime("%H:%M")
    failures = "、".join(readiness_errors) if readiness_errors else "暂无"
    status = "完整" if strict_ready else "不完整"
    return [
        "## 数据质量",
        (
            f"- 数据：{status}；失败项：{failures}；时间 {data_time}；"
            f"覆盖 板块{len(summary.dataset.boards)}、个股{len(summary.dataset.stocks)}、"
            f"指数{len(summary.dataset.indexes)}、外围{len(summary.dataset.externals)}、"
            f"新闻{len(news_items)}、持仓{len(positions)}"
        ),
        "",
    ]


def _personal_market_section(summary: MarketSummary) -> list[str]:
    return [
        "## 市场状态",
        f"- 盘面：{summary.market_state}；{_brief_index_text(summary)}",
        f"- 广度：{_brief_breadth_text(summary)}",
        f"- 资金：{_brief_fund_text(summary)}",
        "",
    ]


def _brief_index_text(summary: MarketSummary) -> str:
    if not summary.dataset.indexes:
        return "暂无"
    return "；".join(f"{item.name} {item.change_pct:.2f}%" for item in summary.dataset.indexes[:5])


def _brief_breadth_text(summary: MarketSummary) -> str:
    breadth = _market_breadth(summary.dataset.stocks)
    return (
        f"全市场成交 {breadth.total_amount / 100000000:.0f}亿，"
        f"上涨家数 {breadth.up_count}，下跌家数 {breadth.down_count}，"
        f"涨停数量 {breadth.limit_up_count}，跌停数量 {breadth.limit_down_count}，"
        f"炸板率 {breadth.failed_limit_up_rate:.2f}%"
    )


def _brief_fund_text(summary: MarketSummary) -> str:
    if not summary.board_views:
        return "暂无"
    inflow = max(summary.board_views, key=lambda item: item.board.main_net_inflow)
    outflow = min(summary.board_views, key=lambda item: item.board.main_net_inflow)
    return (
        f"最强流入 {inflow.board.name} {inflow.board.main_net_inflow / 100000000:.2f}亿；"
        f"最强流出 {outflow.board.name} {outflow.board.main_net_inflow / 100000000:.2f}亿"
    )


def _personal_mainline_section(
    mainline: list[BoardView],
    rebound: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
) -> list[str]:
    return [
        "## 主线判断",
        f"- 主线：{_board_names(mainline)}；反弹：{_board_names(rebound)}；回避：{_board_names((cashout + one_day)[:5])}",
        f"- 结论：{'围绕主升观察' if mainline else '无清晰主线，不硬做'}",
        "",
    ]


def _personal_hot_boards_section(hot_boards: list[BoardView]) -> list[str]:
    rows = ["## 当天热门板块"]
    if not hot_boards:
        rows.extend(["暂无。", ""])
        return rows
    rows.extend(["| 板块 | 今日 | 20日涨幅 | 成交变化 | 标签 |", "|---|---:|---:|---:|---|"])
    for item in hot_boards[:3]:
        rows.append(
            f"| {item.board.name} | {item.board.change_pct:.2f}% | "
            f"{item.board.twenty_day_return:.2f}% | {item.board.volume_ratio:.2f} | {item.label} |"
        )
    rows.append("")
    return rows


def _brief_external_section(
    summary: MarketSummary,
    mainline: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
) -> list[str]:
    positive = [item for item in summary.dataset.externals if item.july_return > 0]
    negative = [item for item in summary.dataset.externals if item.july_return < 0]
    tech_boards = _tech_board_views(mainline + cashout + one_day)
    rows = ["## 外围市场"]
    rows.append(
        f"- 共振：正向 {len(positive)}/{len(summary.dataset.externals)}（{_external_names(positive)}）；"
        f"负向 {len(negative)}/{len(summary.dataset.externals)}（{_external_names(negative)}）"
    )
    rows.append(f"- 处理：{_external_action(positive, negative, tech_boards)}")
    rows.append("")
    return rows


def _news_impact_section(summary: MarketSummary, news_items: list[NewsItem]) -> list[str]:
    rows = ["## 消息驱动分析"]
    rows.append(f"- 消息来源：{_news_source_text(news_items)}；新闻总数 {len(news_items)}")
    rows.append("- 口径：只按新闻标题、摘要和关联对象解释可能影响；没有直接命中就写暂无明确消息证据。")
    for view in _impact_board_views(summary):
        rows.append(_board_cause_line(view, news_items))
    if len(summary.board_views) == 0:
        rows.append("- 板块：暂无；可能原因：暂无明确消息证据；可能影响：暂无。")
    rows.append("")
    return rows


def _morning_scenario_section(
    summary: MarketSummary,
    news_items: list[NewsItem],
    mainline: list[BoardView],
    rebound: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
) -> list[str]:
    positive_external = [item for item in summary.dataset.externals if item.july_return > 0]
    negative_external = [item for item in summary.dataset.externals if item.july_return < 0]
    tech_boards = _tech_board_views(mainline + cashout + one_day)
    focus = _scenario_focus_board(summary, mainline, rebound)
    risk = _scenario_risk_board(summary, cashout, one_day)
    rows = [
        "## 早盘走势推演",
        (
            f"- 外围表现：正向 {len(positive_external)}/{len(summary.dataset.externals)}"
            f"（{_external_names(positive_external)}）；负向 {len(negative_external)}/{len(summary.dataset.externals)}"
            f"（{_external_names(negative_external)}）；处理：{_external_action(positive_external, negative_external, tech_boards)}"
        ),
    ]
    rows.extend(_news_board_impact_lines(summary, news_items))
    rows.extend(
        [
            f"- 高开：不追高，先看{focus}是否继续放量；没有持续资金就不扩大试错。",
            f"- 低开：先看{focus}是否抗跌；{risk}资金未回流前不补仓、不追反弹。",
            f"- 震荡：只观察主线扩散和资金回流；没有主升确认就维持防守。",
        ]
    )
    rows.append("")
    return rows


def _impact_board_views(summary: MarketSummary) -> list[BoardView]:
    if not summary.board_views:
        return []
    candidates = [
        max(summary.board_views, key=lambda item: item.board.change_pct),
        max(summary.board_views, key=lambda item: item.board.main_net_inflow),
        min(summary.board_views, key=lambda item: item.board.main_net_inflow),
    ]
    result: list[BoardView] = []
    seen: set[str] = set()
    for item in candidates:
        if item.board.name not in seen:
            result.append(item)
            seen.add(item.board.name)
    return result


def _board_cause_line(view: BoardView, news_items: list[NewsItem]) -> str:
    matched = _matched_news_for_board(view.board.name, news_items)
    board_text = (
        f"涨跌幅 {view.board.change_pct:.2f}%，主力净流入 {view.board.main_net_inflow / 100000000:.2f}亿，"
        f"上涨/下跌 {view.board.up_count}/{view.board.down_count}，标签 {view.label}"
    )
    if not matched:
        return (
            f"- {view.board.name}：可能原因：暂无明确消息证据；盘面证据：{board_text}；"
            f"可能影响：只能按资金和价格处理；交易含义：不把它当作新闻驱动交易依据。"
        )
    evidence = "；".join(_format_board_news(item, view.board.name) for item in matched[:2])
    return (
        f"- {view.board.name}：可能原因：{evidence}；盘面证据：{board_text}；"
        f"可能影响：{view.board.name}；交易含义：消息与资金同向时看持续性，不追高。"
    )


def _matched_news_for_board(board_name: str, news_items: list[NewsItem]) -> list[NewsItem]:
    return [item for item in news_items if board_name in _news_text(item)]


def _format_board_news(item: NewsItem, board_name: str) -> str:
    return (
        f"{item.source} {item.published_at:%H:%M}《{item.title}》"
        f"（{_news_direction(item)}，{_board_news_evidence(item, board_name)}）"
    )


def _news_text(item: NewsItem) -> str:
    return f"{item.title} {item.summary} {' '.join(item.related_names)}"


def _board_news_evidence(item: NewsItem, board_name: str) -> str:
    evidence: list[str] = []
    if board_name in item.title:
        evidence.append(f"标题包含：{board_name}")
    if board_name in item.summary:
        evidence.append(f"摘要包含：{board_name}")
    if board_name in item.related_names:
        evidence.append(f"关联对象包含：{board_name}")
    return "，".join(evidence) or "无法给出结构化匹配依据"


def _news_direction(item: NewsItem) -> str:
    text = _news_text(item)
    positive = any(keyword in text for keyword in POSITIVE_NEWS_KEYWORDS)
    negative = any(keyword in text for keyword in NEGATIVE_NEWS_KEYWORDS)
    if positive and negative:
        return "多空混杂"
    if positive:
        return "偏利好"
    if negative:
        return "偏利空"
    return "方向未明"


def _news_source_text(news_items: list[NewsItem]) -> str:
    if not news_items:
        return "暂无"
    return "、".join(sorted({item.source for item in news_items}))


def _news_board_impact_lines(summary: MarketSummary, news_items: list[NewsItem]) -> list[str]:
    lines: list[str] = []
    for view in summary.board_views:
        if view.label != "主升候选":
            continue
        matched = _matched_news_for_board(view.board.name, news_items)
        if not matched:
            continue
        direction = _combined_news_direction(matched)
        if direction != "偏利好":
            continue
        link = _first_news_link(matched)
        if not link:
            continue
        watch_stocks = _suitable_watch_stocks(summary, view.board.name, matched)
        if not watch_stocks:
            continue
        lines.append(
            f"- 消息影响：{view.board.name}：{direction}；"
            f"原因：{_positive_news_reason_text(matched)}；"
            f"建仓观察：{_watch_stock_names(watch_stocks)}；"
            f"高位判断：不在高位（日涨 {_stock_change_text(watch_stocks)}）；"
            f"适合建仓观察；触发：{_watchlist_buy_trigger(watch_stocks[0])}；"
            f"链接：{link}"
        )
    return lines


def _combined_news_direction(news_items: list[NewsItem]) -> str:
    directions = {_news_direction(item) for item in news_items}
    if "偏利好" in directions and "偏利空" in directions:
        return "多空混杂"
    if "偏利好" in directions:
        return "偏利好"
    if "偏利空" in directions:
        return "偏利空"
    if "多空混杂" in directions:
        return "多空混杂"
    return "方向未明"


def _suitable_watch_stocks(summary: MarketSummary, board_name: str, news_items: list[NewsItem]) -> list[StockSnapshot]:
    news_text = " ".join(_news_text(item) for item in news_items)
    candidates = [
        stock
        for stock in summary.dataset.stocks
        if stock.industry == board_name or stock.name in news_text
    ]
    candidates = [stock for stock in candidates if _is_suitable_entry_stock(stock)]
    candidates.sort(key=lambda stock: stock.amount, reverse=True)
    return candidates[:3]


def _is_suitable_entry_stock(stock: StockSnapshot) -> bool:
    if stock.amount <= 0 or stock.change_pct <= 0:
        return False
    return stock.change_pct <= 5.0


def _watch_stock_names(stocks: list[StockSnapshot]) -> str:
    return "、".join(f"{stock.name}({stock.code})" for stock in stocks)


def _stock_change_text(stocks: list[StockSnapshot]) -> str:
    return "、".join(f"{stock.name} {stock.change_pct:.2f}%" for stock in stocks)


def _first_news_link(news_items: list[NewsItem]) -> str:
    for item in news_items:
        if item.url.strip():
            return item.url.strip()
    return ""


def _positive_news_reason_text(news_items: list[NewsItem]) -> str:
    text = " ".join(_news_text(item) for item in news_items)
    reasons: list[str] = []
    if "政策" in text or "支持" in text:
        reasons.append("政策支持")
    if "订单" in text or "中标" in text:
        reasons.append("订单/中标")
    if "增长" in text or "预增" in text or "同比增长" in text:
        reasons.append("增长预期")
    if "涨价" in text:
        reasons.append("涨价预期")
    if "注册证书" in text or "获批" in text:
        reasons.append("审批进展")
    return "、".join(dict.fromkeys(reasons)) if reasons else "新闻利好命中板块"


def _scenario_focus_board(
    summary: MarketSummary,
    mainline: list[BoardView],
    rebound: list[BoardView],
) -> str:
    if mainline:
        return mainline[0].board.name
    if rebound:
        return rebound[0].board.name
    if summary.board_views:
        return max(summary.board_views, key=lambda item: item.board.main_net_inflow).board.name
    return "强势方向"


def _scenario_risk_board(
    summary: MarketSummary,
    cashout: list[BoardView],
    one_day: list[BoardView],
) -> str:
    if cashout:
        return cashout[0].board.name
    if one_day:
        return one_day[0].board.name
    if summary.board_views:
        return min(summary.board_views, key=lambda item: item.board.main_net_inflow).board.name
    return "弱势方向"


def _brief_position_section(
    summary: MarketSummary,
    positions: list[PositionSnapshot],
    news_items: list[NewsItem],
    focus_positions: bool,
) -> list[str]:
    rows = ["## 持仓操作建议"]
    if not focus_positions:
        rows.extend(["本阶段暂不关注持仓个股。", ""])
        return rows
    if not positions:
        rows.extend(["未提供真实持仓文件，不生成逐股建议。", ""])
        return rows
    rows.extend(_position_section(summary, positions, news_items, focus_positions)[1:])
    return rows


def _brief_watchlist_section(candidates: list[tuple[StockSnapshot, BoardView]], mainline: list[BoardView]) -> list[str]:
    rows = ["## 建仓候选"]
    if not mainline:
        rows.extend(["暂无满足主升条件的建仓候选。", ""])
        return rows
    if not candidates:
        rows.extend(["有主升板块，但没有满足个股强度和成交要求的标的。", ""])
        return rows
    rows.extend(["| 标的 | 板块 | 触发 | 失效 |", "|---|---|---|---|"])
    for stock, board in candidates[:3]:
        rows.append(
            f"| {stock.name}({stock.code}) | {board.board.name} | "
            f"{_watchlist_buy_trigger(stock)} | 板块跌出主升或个股破位 |"
        )
    rows.append("")
    return rows


def _brief_news_section(
    positions: list[PositionSnapshot],
    mainline: list[BoardView],
    news_items: list[NewsItem],
    watchlist_stocks: list[StockSnapshot],
) -> list[str]:
    rows = ["## 利好利空信息"]
    matched = match_news(positions, mainline, news_items, watchlist_stocks)
    if not matched:
        rows.extend([f"暂无匹配消息。新闻总数：{len(news_items)}。", ""])
        return rows
    rows.extend(["| 时间 | 标题 | 对象 |", "|---|---|---|"])
    for item, targets in matched[:3]:
        rows.append(f"| {item.published_at:%H:%M} | {item.title} | {'、'.join(targets)} |")
    rows.append("")
    return rows


def _personal_operation_section(
    summary: MarketSummary,
    mainline: list[BoardView],
    rebound: list[BoardView],
    cashout: list[BoardView],
) -> list[str]:
    reason = "暂无主升候选，优先控制仓位"
    if mainline:
        reason = "已有主升候选，只围绕强势方向观察"
    elif cashout and rebound:
        reason = "高位兑现和低位反弹并存，追涨风险高"
    return [
        "## 今日操作建议",
        f"- 策略：{summary.operation_advice}；原因：{reason}",
        "",
    ]


def _quality_section(
    summary: MarketSummary,
    positions: list[PositionSnapshot],
    news_items: list[NewsItem],
    strict_ready: bool,
    readiness_errors: list[str],
) -> list[str]:
    data_time = summary.dataset.fetched_at.strftime("%Y-%m-%d %H:%M:%S")
    sources = "、".join(summary.dataset.data_sources) if summary.dataset.data_sources else "未记录"
    failures = "、".join(readiness_errors) if readiness_errors else "暂无"
    return [
        "## 数据质量",
        f"- 数据源：{sources}",
        f"- 数据时间：{data_time}",
        f"- 严格完整：{'是' if strict_ready else '否'}",
        f"- 板块数量：{len(summary.dataset.boards)}",
        f"- 个股数量：{len(summary.dataset.stocks)}",
        f"- 指数数量：{len(summary.dataset.indexes)}",
        f"- 外围指标数量：{len(summary.dataset.externals)}",
        f"- 持仓数量：{len(positions)}",
        f"- 新闻数量：{len(news_items)}",
        f"- 失败项：{failures}",
        "- 数据原则：关键数据缺失时直接暴露，不用假数据补齐",
        "",
    ]


def _personal_evening_section(
    summary: MarketSummary,
    mainline: list[BoardView],
    rebound: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
) -> list[str]:
    return [
        "## 晚间复盘",
        (
            f"- 复盘：{summary.market_state}；最强 {_board_names(mainline) if mainline else '暂无明确主升'}；"
            f"失败/回避 {_board_names((cashout + one_day)[:5])}；"
            f"结构 反弹{len(rebound)}、高位兑现{len(cashout)}、一日游{len(one_day)}"
        ),
        "",
    ]


def _personal_next_day_section(
    summary: MarketSummary,
    mainline: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
    watchlist_candidates: list[tuple[StockSnapshot, BoardView]],
) -> list[str]:
    return [
        "## 次日操作计划",
        f"- 次日：高开不追；低开看抗跌；震荡只观察主线扩散；破位降仓暂停新增。",
        f"- 关注：板块 {_board_names(mainline) if mainline else '暂无明确主升'}；个股 {_key_stock_text(watchlist_candidates)}；风险 {_board_names((cashout + one_day)[:3])}；动作 {summary.operation_advice}",
        "",
    ]


def _personal_risk_section(
    positions: list[PositionSnapshot],
    mainline: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
    watchlist_candidates: list[tuple[StockSnapshot, BoardView]],
    focus_positions: bool,
) -> list[str]:
    return [
        "## 风险提示",
        (
            f"- 风险：持仓 {('暂不关注' if not focus_positions else _position_risk_row(positions, True).split('|')[2].strip())}；"
            f"主线 {'无主升候选，暂停新增' if not mainline else '只围绕主升小仓试错'}；"
            f"退潮 {_board_names((cashout + one_day)[:5])}；"
            f"建仓 {'暂无候选' if not watchlist_candidates else '只等触发'}；人工确认"
        ),
        "",
    ]


def _market_section(
    summary: MarketSummary,
    mainline: list[BoardView],
    rebound: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
) -> list[str]:
    rows = [
        "## 市场状态",
        f"- 状态：{summary.market_state}",
        f"- 主升候选数量：{len(mainline)}",
        f"- 反弹候选数量：{len(rebound)}",
        f"- 高位兑现数量：{len(cashout)}",
        f"- 一日游风险数量：{len(one_day)}",
    ]
    rows.extend(_index_table(summary))
    rows.extend(_breadth_table(summary))
    rows.append("")
    return rows


def _mainline_judgement_section(
    mainline: list[BoardView],
    rebound: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
) -> list[str]:
    rows = ["## 主线判断", "| 判断项 | 方向 | 依据 |", "|---|---|---|"]
    for label, items in [
        ("主升候选", mainline),
        ("反弹候选", rebound),
        ("高位兑现", cashout),
        ("一日游风险", one_day),
    ]:
        rows.append(f"| {label} | {_board_names(items)} | {_board_reasons(items)} |")
    if mainline:
        rows.append("| 无清晰主线 | 否 | 已出现主升候选 |")
    else:
        rows.append("| 无清晰主线 | 是 | 暂无明确主升候选 |")
    rows.append("")
    return rows


def _board_names(items: list[BoardView]) -> str:
    if not items:
        return "暂无"
    return "、".join(item.board.name for item in items[:5])


def _board_reasons(items: list[BoardView]) -> str:
    if not items:
        return "暂无"
    return "；".join(f"{item.board.name}：{item.reason}" for item in items[:3])


def _index_table(summary: MarketSummary) -> list[str]:
    rows = ["", "| 指数 | 点位 | 涨跌幅 | 成交额 |", "|---|---:|---:|---:|"]
    for item in summary.dataset.indexes:
        rows.append(f"| {item.name} | {item.price:.2f} | {item.change_pct:.2f}% | {item.amount / 100000000:.2f}亿 |")
    return rows


@dataclass(frozen=True)
class MarketBreadth:
    total_amount: float
    up_count: int
    down_count: int
    flat_count: int
    limit_up_count: int
    limit_down_count: int
    failed_limit_up_count: int

    @property
    def failed_limit_up_rate(self) -> float:
        touched = self.limit_up_count + self.failed_limit_up_count
        if touched == 0:
            return 0.0
        return round(self.failed_limit_up_count / touched * 100, 2)


def _breadth_table(summary: MarketSummary) -> list[str]:
    breadth = _market_breadth(summary.dataset.stocks)
    return [
        "",
        "| 全市场成交 | 上涨家数 | 下跌家数 | 平盘家数 | 涨停数量 | 跌停数量 | 炸板率 |",
        "|---:|---:|---:|---:|---:|---:|---:|",
        f"| {breadth.total_amount / 100000000:.2f}亿 | {breadth.up_count} | {breadth.down_count} | "
        f"{breadth.flat_count} | {breadth.limit_up_count} | {breadth.limit_down_count} | "
        f"{breadth.failed_limit_up_rate:.2f}% |",
    ]


def _market_breadth(stocks: list[StockSnapshot]) -> MarketBreadth:
    up_count = sum(1 for stock in stocks if stock.change_pct > 0)
    down_count = sum(1 for stock in stocks if stock.change_pct < 0)
    flat_count = sum(1 for stock in stocks if stock.change_pct == 0)
    limit_up_count = sum(1 for stock in stocks if _is_limit_up(stock))
    limit_down_count = sum(1 for stock in stocks if _is_limit_down(stock))
    failed_limit_up_count = sum(1 for stock in stocks if _touched_limit_up(stock) and not _is_limit_up(stock))
    return MarketBreadth(
        total_amount=sum(stock.amount for stock in stocks),
        up_count=up_count,
        down_count=down_count,
        flat_count=flat_count,
        limit_up_count=limit_up_count,
        limit_down_count=limit_down_count,
        failed_limit_up_count=failed_limit_up_count,
    )


def _is_limit_up(stock: StockSnapshot) -> bool:
    limit_price = _limit_price(stock.previous_close, _limit_rate(stock), 1)
    return stock.price >= limit_price if limit_price else False


def _is_limit_down(stock: StockSnapshot) -> bool:
    limit_price = _limit_price(stock.previous_close, _limit_rate(stock), -1)
    return stock.price <= limit_price if limit_price else False


def _touched_limit_up(stock: StockSnapshot) -> bool:
    limit_price = _limit_price(stock.previous_close, _limit_rate(stock), 1)
    return stock.high >= limit_price if limit_price else False


def _limit_rate(stock: StockSnapshot) -> Decimal:
    if "ST" in stock.name.upper():
        return Decimal("0.05")
    if stock.code.startswith(("300", "301", "302", "688", "689")):
        return Decimal("0.20")
    if stock.code.startswith(("8", "4")):
        return Decimal("0.30")
    return Decimal("0.10")


def _limit_price(previous_close: float, rate: Decimal, direction: int) -> float | None:
    if previous_close <= 0:
        return None
    base = Decimal(str(previous_close))
    multiplier = Decimal("1") + rate if direction > 0 else Decimal("1") - rate
    return float((base * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _operation_section(
    summary: MarketSummary,
    mainline: list[BoardView],
    rebound: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
) -> list[str]:
    reason = "暂无主升候选，优先控制仓位"
    if mainline:
        reason = "已有主升候选，只围绕强势方向观察"
    elif cashout and rebound:
        reason = "高位兑现和低位反弹并存，追涨风险高"
    return [
        "## 今日操作建议",
        f"- 建议：{summary.operation_advice}",
        f"- 原因：{reason}",
        "",
    ]


def _external_resonance_section(
    summary: MarketSummary,
    mainline: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
) -> list[str]:
    tech_externals = summary.dataset.externals
    positive = [item for item in tech_externals if item.july_return > 0]
    negative = [item for item in tech_externals if item.july_return < 0]
    tech_boards = _tech_board_views(mainline + cashout + one_day)
    rows = ["## 外围共振", "| 项目 | 数据 | 判断 |", "|---|---|---|"]
    rows.append(f"| 外围科技正向 | {len(positive)}/{len(tech_externals)} | {_external_names(positive)} |")
    rows.append(f"| 外围科技负向 | {len(negative)}/{len(tech_externals)} | {_external_names(negative)} |")
    rows.append(f"| A股科技状态 | {_board_names(tech_boards)} | {_board_reasons(tech_boards)} |")
    rows.append(f"| 处理 | {summary.operation_advice} | {_external_action(positive, negative, tech_boards)} |")
    rows.append("")
    return rows


def _tech_board_views(items: list[BoardView]) -> list[BoardView]:
    keywords = ("科技", "半导体", "芯片", "封装", "存储", "集成电路", "电子", "通信", "中芯")
    return [item for item in items if any(keyword in item.board.name for keyword in keywords)]


def _external_names(items: list) -> str:
    if not items:
        return "暂无"
    return "、".join(item.name for item in items[:5])


def _external_action(positive: list, negative: list, tech_boards: list[BoardView]) -> str:
    risky_tech = [item for item in tech_boards if item.label in {"高位兑现", "一日游风险"}]
    if len(negative) > len(positive) and risky_tech:
        return "外围偏弱，A股科技不按主升处理"
    if len(positive) > len(negative) and tech_boards:
        return "外围偏强，但仍以A股主线判断为准"
    if not tech_boards:
        return "A股科技无明确主线，不单独因外围建仓"
    return "外围分歧，等待A股板块持续性确认"


def _position_section(
    summary: MarketSummary,
    positions: list[PositionSnapshot],
    news_items: list[NewsItem],
    focus_positions: bool,
) -> list[str]:
    rows = ["## 持仓操作建议"]
    if not focus_positions:
        rows.extend(["本阶段暂不关注持仓个股，不生成逐股止盈、止损、减仓、补仓、清仓建议。", ""])
        return rows
    if not positions:
        rows.extend(_missing_position_rows())
        return rows
    advice_items = [_build_position_advice(summary, position) for position in positions]
    news_by_position = _position_news_map(positions, news_items)
    rows.extend(_position_sizing_section(summary, advice_items))
    rows.extend(
        [
            "| 股票 | 数量 | 成本 | 现价 | 浮盈亏 | 板块状态 | 建议 | 止损 | 止盈 | 减仓条件 | 补仓条件 | 清仓条件 | 利好利空 |",
            "|---|---:|---:|---:|---:|---|---|---|---|---|---|---|---|",
        ]
    )
    for advice in advice_items:
        news_text = "；".join(news_by_position.get(advice.name, [])) or "暂无匹配消息"
        rows.append(
            f"| {advice.name}({advice.code}) | {advice.quantity} | {advice.cost_price:.2f} | "
            f"{advice.current_price:.2f} | {advice.pnl_pct:.2f}% | {advice.board_label} | "
            f"{advice.action} | {advice.stop_loss} | {advice.take_profit} | "
            f"{advice.reduce_condition} | {advice.add_condition} | {advice.clear_condition} | {news_text} |"
        )
    rows.append("")
    return rows


def _position_sizing_section(summary: MarketSummary, advice_items: list[PositionAdvice]) -> list[str]:
    clear_names = _advice_names(advice_items, "清仓")
    reduce_names = _advice_names(advice_items, "减仓")
    observe_names = _advice_names(advice_items, "观察")
    hold_names = _advice_names(advice_items, "持有")
    if clear_names != "暂无":
        policy = f"先处理清仓标的：{clear_names}；不补仓，不新增仓位。"
    elif reduce_names != "暂无":
        policy = f"先处理减仓标的：{reduce_names}；不补仓，等待主线重新确认。"
    else:
        policy = "维持现有仓位，不追高；只有主升候选重新出现才考虑新增。"
    return [
        "## 仓位调整建议",
        f"- 总体：{summary.operation_advice}。{policy}",
        f"- 分组：清仓 {clear_names}；减仓 {reduce_names}；观察 {observe_names}；持有 {hold_names}",
        "",
    ]


def _advice_names(advice_items: list[PositionAdvice], action: str) -> str:
    names = [f"{item.name}({item.code})" for item in advice_items if item.action == action]
    return "、".join(names) if names else "暂无"


def _missing_position_rows() -> list[str]:
    return [
        "未提供真实持仓文件，不能生成逐股止盈、止损、减仓、补仓、清仓建议。",
        "",
        "## 持仓输入要求",
        "| 项目 | 内容 |",
        "|---|---|",
        "| 正式路径 | data/positions.csv |",
        "| 必填字段 | code、name、quantity、cost_price |",
        "| 可选字段 | buy_date、available_quantity、position_value、note |",
        "| 示例文件 | data/positions.example.csv 只能参考，不能直接用于正式报告 |",
        "| 就绪检查 | python -m personal_quant.cli check-inputs --as-of 2026-07-09 --positions data/positions.csv --output-dir reports |",
        "",
        "## 持仓补齐后输出",
        "| 输出项 | 内容 |",
        "|---|---|",
        "| 浮盈浮亏 | 按真实成本价、现价和数量计算 |",
        "| 操作建议 | 持有、减仓、补仓、清仓、观察 |",
        "| 触发条件 | 止盈、止损、减仓、补仓、清仓条件 |",
        "| 利好利空 | 匹配持仓个股相关新闻，显示时间、来源、标题、影响方向和匹配依据 |",
        "",
    ]


def _position_news_map(positions: list[PositionSnapshot], news_items: list[NewsItem]) -> dict[str, list[str]]:
    names = {position.name for position in positions}
    result = {name: [] for name in names}
    for item in news_items:
        text = f"{item.title} {item.summary}"
        for name in names:
            if name in text:
                result[name].append(_format_news_impact(item, [name]))
    return result


def _format_news_impact(item: NewsItem, targets: list[str]) -> str:
    return (
        f"{item.published_at:%Y-%m-%d %H:%M} {item.source} {item.title} "
        f"影响方向：无法判断 匹配依据：{_news_evidence(item, targets)}"
    )


def _watchlist_section(candidates: list[tuple[StockSnapshot, BoardView]], mainline: list[BoardView]) -> list[str]:
    rows = ["## 建仓候选"]
    if not mainline:
        rows.extend(["暂无满足主升条件的建仓候选。", ""])
        return rows
    if not candidates:
        rows.extend(["存在主升板块，但没有找到同时满足个股强度和成交要求的标的。", ""])
        return rows
    rows.extend(
        [
            "| 候选 | 所属板块 | 今日涨幅 | 成交额 | 角色 | 选择原因 | 买入触发 | 失效条件 | 仓位上限 | 风险提示 |",
            "|---|---|---:|---:|---|---|---|---|---|---|",
        ]
    )
    for stock, board in candidates:
        rows.append(
            f"| {stock.name}({stock.code}) | {board.board.name} | {stock.change_pct:.2f}% | "
            f"{stock.amount / 100000000:.2f}亿 | 趋势核心 | {_watchlist_reason(stock, board)} | "
            f"{_watchlist_buy_trigger(stock)} | "
            f"板块退出主升候选或个股跌破 5 日趋势 | 单票不超过总仓 10% |"
            f" {_watchlist_risk_note()} |"
        )
    rows.append("")
    return rows


def _watchlist_reason(stock: StockSnapshot, board: BoardView) -> str:
    return (
        f"板块：{board.reason}；个股涨幅 {stock.change_pct:.2f}%，"
        f"成交额 {stock.amount / 100000000:.2f}亿"
    )


def _watchlist_buy_trigger(stock: StockSnapshot) -> str:
    return f"突破今日高点 {stock.high:.2f} 或回踩不破前收 {stock.previous_close:.2f} 后再观察"


def _watchlist_risk_note() -> str:
    return "未接入解禁、减持、业绩兑现、龙虎榜数据，不能视为已排除风险"


def _news_section(
    positions: list[PositionSnapshot],
    mainline: list[BoardView],
    news_items: list[NewsItem],
    watchlist_stocks: list[StockSnapshot],
) -> list[str]:
    matched = match_news(positions, mainline, news_items, watchlist_stocks)
    rows = _news_match_status_section(positions, mainline, news_items, watchlist_stocks, len(matched))
    rows.append("## 利好利空信息")
    if not matched:
        rows.extend(["暂无已匹配到持仓、建仓候选或主升板块的利好利空信息。", ""])
        return rows
    rows.extend(["| 时间 | 来源 | 标题 | 影响对象 | 影响方向 | 匹配依据 |", "|---|---|---|---|---|---|"])
    for item, targets in matched[:10]:
        rows.append(
            f"| {item.published_at:%Y-%m-%d %H:%M} | {item.source} | {item.title} | "
            f"{'、'.join(targets)} | 无法判断 | {_news_evidence(item, targets)} |"
        )
    rows.append("")
    return rows


def _news_match_status_section(
    positions: list[PositionSnapshot],
    mainline: list[BoardView],
    news_items: list[NewsItem],
    watchlist_stocks: list[StockSnapshot],
    matched_count: int,
) -> list[str]:
    target_text = f"持仓 {len(positions)} 个、主升板块 {len(mainline)} 个、建仓候选 {len(watchlist_stocks)} 个"
    match_result = f"已匹配 {matched_count} 条" if matched_count else "未匹配"
    reason = "已进入利好利空表" if matched_count else "没有新闻同时命中持仓、主升板块或建仓候选"
    action = "只做证据提示，影响方向无结构化证据时为无法判断" if matched_count else "不作为交易依据"
    return [
        "## 新闻匹配状态",
        "| 项目 | 内容 |",
        "|---|---|",
        f"| 新闻总数 | {len(news_items)} |",
        f"| 匹配对象 | {target_text} |",
        f"| 匹配结果 | {match_result} |",
        f"| 未匹配原因 | {reason} |",
        f"| 处理 | {action} |",
        "",
    ]


def _news_evidence(item: NewsItem, targets: list[str]) -> str:
    evidence: list[str] = []
    for target in targets:
        if target in item.title:
            evidence.append(f"标题包含：{target}")
        elif target in item.summary:
            evidence.append(f"摘要包含：{target}")
    return "；".join(evidence) or "无法给出结构化匹配依据"


def _evening_section(
    summary: MarketSummary,
    mainline: list[BoardView],
    rebound: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
) -> list[str]:
    strongest = "、".join(item.board.name for item in mainline[:3]) if mainline else "暂无明确主升方向"
    weak_signal = "、".join(item.board.name for item in (cashout + one_day)[:3]) or "暂无明显退潮信号"
    rows = [
        "## 晚间复盘",
        f"- 当天结论：{summary.market_state}",
        f"- 最强方向：{strongest}",
        f"- 需要回避：{weak_signal}",
        f"- 复盘判断：反弹候选 {len(rebound)} 个，高位兑现 {len(cashout)} 个，一日游风险 {len(one_day)} 个。",
        "",
    ]
    rows.extend(_failed_direction_section(cashout, one_day))
    rows.extend(_fund_flow_section(summary))
    return rows


def _failed_direction_section(cashout: list[BoardView], one_day: list[BoardView]) -> list[str]:
    failed = (cashout + one_day)[:5]
    rows = ["## 失败方向"]
    if not failed:
        rows.extend(["暂无明确失败方向。", ""])
        return rows
    rows.extend(["| 方向 | 类型 | 原因 |", "|---|---|---|"])
    for item in failed:
        rows.append(f"| {item.board.name} | {item.label} | {item.reason} |")
    rows.append("")
    return rows


def _fund_flow_section(summary: MarketSummary) -> list[str]:
    return _fund_flow_rows("## 资金状态", summary)


def _fund_strength_section(summary: MarketSummary) -> list[str]:
    return _fund_flow_rows("## 资金强弱", summary)


def _fund_flow_rows(title: str, summary: MarketSummary) -> list[str]:
    boards = sorted(summary.board_views, key=lambda item: item.board.main_net_inflow, reverse=True)
    inflow = boards[:3]
    outflow = sorted(summary.board_views, key=lambda item: item.board.main_net_inflow)[:3]
    rows = [title, "| 类型 | 方向 | 主力净流入 | 标签 |", "|---|---|---:|---|"]
    for item in inflow:
        rows.append(f"| 流入靠前 | {item.board.name} | {item.board.main_net_inflow / 100000000:.2f}亿 | {item.label} |")
    for item in outflow:
        rows.append(f"| 流出靠前 | {item.board.name} | {item.board.main_net_inflow / 100000000:.2f}亿 | {item.label} |")
    rows.append("")
    return rows


def _next_day_section(
    summary: MarketSummary,
    mainline: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
    watchlist_candidates: list[tuple[StockSnapshot, BoardView]],
) -> list[str]:
    key_boards = "、".join(item.board.name for item in mainline[:3]) or "暂无明确主升方向"
    key_risk = "、".join(item.board.name for item in (cashout + one_day)[:3]) or "暂无明显退潮信号"
    key_stocks = _key_stock_text(watchlist_candidates)
    key_prices = _key_price_text(watchlist_candidates)
    rows = [
        "## 次日操作计划",
        "| 情景 | 操作 |",
        "|---|---|",
        f"| 高开 | 不追高，先看热门板块是否继续放量；{summary.operation_advice} |",
        "| 低开 | 观察强势板块是否抗跌，弱势持仓优先执行止损纪律 |",
        "| 震荡 | 只处理已有持仓，不扩大试错范围 |",
        "| 破位 | 降低仓位，暂停新增建仓候选 |",
        "",
    ]
    rows.extend(
        [
            "## 明日关注",
            "| 项目 | 内容 |",
            "|---|---|",
            f"| 关键板块 | {key_boards} |",
            f"| 关键个股 | {key_stocks} |",
            f"| 关键价位 | {key_prices} |",
            f"| 关键风险 | {key_risk} |",
            f"| 关键动作 | {summary.operation_advice} |",
            "",
        ]
    )
    return rows


def _key_stock_text(watchlist_candidates: list[tuple[StockSnapshot, BoardView]]) -> str:
    if not watchlist_candidates:
        return "暂无满足主升条件的关键个股"
    return "、".join(f"{stock.name}({stock.code})" for stock, _ in watchlist_candidates[:3])


def _key_price_text(watchlist_candidates: list[tuple[StockSnapshot, BoardView]]) -> str:
    if not watchlist_candidates:
        return "暂无建仓候选，暂不设建仓价位"
    parts = []
    for stock, _ in watchlist_candidates[:3]:
        parts.append(f"{stock.name}：突破今日高点 {stock.high:.2f} 或回踩不破前收 {stock.previous_close:.2f}")
    return "；".join(parts)


def _position_next_day_section(
    summary: MarketSummary,
    positions: list[PositionSnapshot],
    focus_positions: bool,
) -> list[str]:
    rows = ["## 持仓次日处理"]
    if not focus_positions:
        rows.extend(["本阶段暂不关注持仓个股，不输出逐股开盘、冲高、下杀、破位处理。", ""])
        return rows
    if not positions:
        rows.extend(["未提供真实持仓文件，不能输出逐股开盘、冲高、下杀、破位处理。", ""])
        return rows
    rows.extend(
        [
            "| 股票 | 开盘 | 冲高 | 下杀 | 破位 |",
            "|---|---|---|---|---|",
        ]
    )
    for advice in [_build_position_advice(summary, position) for position in positions]:
        rows.append(
            f"| {advice.name}({advice.code}) | 按“{advice.action}”执行，先看板块是否仍为{advice.board_label} | "
            f"{advice.take_profit} | {advice.stop_loss} | {advice.clear_condition} |"
        )
    rows.append("")
    return rows


def _risk_section(
    positions: list[PositionSnapshot],
    mainline: list[BoardView],
    cashout: list[BoardView],
    one_day: list[BoardView],
    watchlist_candidates: list[tuple[StockSnapshot, BoardView]],
    focus_positions: bool,
) -> list[str]:
    rows = ["## 风险提示", "| 风险项 | 当前状态 | 处理 |", "|---|---|---|"]
    rows.append(_position_risk_row(positions, focus_positions))
    rows.append(_mainline_risk_row(mainline))
    rows.append(_failed_direction_risk_row(cashout, one_day))
    rows.append(_watchlist_risk_row(watchlist_candidates))
    rows.append(
        "| 事件风险 | 未接入解禁、减持、业绩兑现、龙虎榜结构化数据 | "
        "不能视为已排除事件风险，有候选也只能等待后续数据确认 |"
    )
    rows.append(
        "| 外围风险 | 外围共振/外围背离已在盘中输出 | "
        "科技链受外围影响时降低试错仓位 |"
    )
    rows.append("| 交易风险 | 本报告不调用券商接口，不会自动下单 | 所有买卖动作必须人工确认 |")
    rows.append("")
    return rows


def _position_risk_row(positions: list[PositionSnapshot], focus_positions: bool) -> str:
    if not focus_positions:
        return "| 持仓风险 | 本阶段暂不关注持仓个股 | 不输出逐股减仓、补仓、清仓建议 |"
    if not positions:
        return "| 持仓风险 | 未提供真实持仓，持仓风险无法逐股评估 | 不输出逐股减仓、补仓、清仓建议 |"
    names = "、".join(position.name for position in positions[:5])
    return f"| 持仓风险 | 已读取真实持仓：{names} | 按持仓操作建议逐股执行止盈、止损、减仓、补仓、清仓条件 |"


def _mainline_risk_row(mainline: list[BoardView]) -> str:
    if not mainline:
        return "| 主线风险 | 无主升候选 | 暂停新增建仓，等待主升候选出现 |"
    return f"| 主线风险 | 主升候选：{_board_names(mainline)} | 只围绕主升方向小仓试错，跌出主升即停止新增 |"


def _failed_direction_risk_row(cashout: list[BoardView], one_day: list[BoardView]) -> str:
    failed = _board_names((cashout + one_day)[:5])
    if failed == "暂无":
        return "| 退潮风险 | 暂无明确高位兑现或一日游方向 | 继续观察是否出现放量回落和板块断层 |"
    return f"| 退潮风险 | {failed} | 回避高位兑现和一日游方向 |"


def _watchlist_risk_row(watchlist_candidates: list[tuple[StockSnapshot, BoardView]]) -> str:
    if not watchlist_candidates:
        return "| 建仓风险 | 暂无建仓候选 | 不硬推标的，不为了交易而交易 |"
    names = "、".join(f"{stock.name}({stock.code})" for stock, _ in watchlist_candidates[:5])
    return f"| 建仓风险 | 候选：{names} | 只在买入触发满足且失效条件未触发时观察 |"


def _build_position_advice(summary: MarketSummary, position: PositionSnapshot) -> PositionAdvice:
    stock_by_code = {stock.code: stock for stock in summary.dataset.stocks}
    stock = stock_by_code.get(position.code)
    if stock is None:
        raise ValueError(f"持仓缺少真实行情: {position.code}")
    board_label = _board_label_for(summary, stock)
    return advise_position(position, stock, board_label)


def _board_label_for(summary: MarketSummary, stock: StockSnapshot) -> str:
    for item in summary.board_views:
        if item.board.name == stock.industry:
            return item.label
    raise ValueError(f"持仓所属板块缺失: {stock.code} {stock.industry}")
