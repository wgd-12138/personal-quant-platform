from __future__ import annotations

from dataclasses import dataclass

from personal_quant.data_center.models import NewsItem, StockSnapshot
from personal_quant.market_analysis.classifier import BoardView, MarketSummary
from personal_quant.watchlist_engine.selector import select_watchlist


@dataclass(frozen=True)
class AuctionSnapshot:
    code: str
    name: str
    auction_price: float
    previous_close: float
    auction_amount: float
    auction_volume_ratio: float


def render_morning_build_report(
    summary: MarketSummary,
    news_items: list[NewsItem],
    auctions: list[AuctionSnapshot] | None = None,
    limit: int = 2,
) -> str:
    rows = ["## 早盘建仓推荐"]
    if auctions is None:
        rows.extend(["暂无适合今日建仓的股票；缺集合竞价数据。", ""])
        return "\n".join(rows) + "\n"
    mainline = [item for item in summary.board_views if item.label == "主升候选"]
    picks: list[tuple[StockSnapshot, BoardView, AuctionSnapshot, NewsItem]] = []
    auctions_by_code = {item.code: item for item in auctions}
    for stock, board in select_watchlist(summary, mainline):
        auction = auctions_by_code.get(stock.code)
        if auction is None:
            continue
        news = _matched_positive_news(news_items, stock, board)
        if news is None:
            continue
        if not _morning_entry_ok(stock, auction):
            continue
        picks.append((stock, board, auction, news))
    if not picks:
        rows.extend(["暂无适合今日建仓的股票；集合竞价、资金、消息面或价格位置不满足。", ""])
        return "\n".join(rows) + "\n"
    for stock, board, auction, news in picks[:limit]:
        entry = auction.auction_price
        rows.append(
            f"- {stock.name}({stock.code})：建仓价 {entry:.2f}；止盈 {_take_profit(entry):.2f}；"
            f"止损 {_stop_loss(entry):.2f}；仓位 单票不超过总仓 10%；"
            f"原因：集合竞价温和高开 {auction_gap_pct(auction):.2f}%，"
            f"竞价量比 {auction.auction_volume_ratio:.2f}，板块 {board.board.name} 为主升候选，"
            f"{_positive_reason(news)}；链接：{news.url}"
        )
    rows.append("")
    return "\n".join(rows) + "\n"


def render_overnight_arbitrage_report(
    summary: MarketSummary,
    news_items: list[NewsItem],
    limit: int = 2,
) -> str:
    rows = ["## 尾盘隔夜套利"]
    mainline = [item for item in summary.board_views if item.label == "主升候选"]
    picks: list[tuple[StockSnapshot, BoardView, NewsItem]] = []
    for stock, board in select_watchlist(summary, mainline):
        news = _matched_positive_news(news_items, stock, board)
        if news is None:
            continue
        if not _tail_entry_ok(stock):
            continue
        picks.append((stock, board, news))
    if not picks:
        rows.extend(["暂无满足隔夜套利条件的标的；不为了交易而交易。", ""])
        return "\n".join(rows) + "\n"
    for stock, board, news in picks[:limit]:
        entry = stock.price
        rows.append(
            f"- {stock.name}({stock.code})：尾盘建仓价 {entry:.2f}；"
            f"次日冲高止盈 {_overnight_take_profit(entry):.2f}；止损 {_stop_loss(entry):.2f}；"
            f"动作 第二天冲高全部抛出；原因：板块 {board.board.name} 为主升候选，"
            f"个股日涨 {stock.change_pct:.2f}% 不在高位，{_positive_reason(news)}；链接：{news.url}"
        )
    rows.append("")
    return "\n".join(rows) + "\n"


def auction_gap_pct(auction: AuctionSnapshot) -> float:
    if auction.previous_close <= 0:
        return 0.0
    return round((auction.auction_price / auction.previous_close - 1) * 100, 2)


def _morning_entry_ok(stock: StockSnapshot, auction: AuctionSnapshot) -> bool:
    gap = auction_gap_pct(auction)
    return 0.5 <= gap <= 4.5 and auction.auction_amount > 0 and auction.auction_volume_ratio >= 1.5 and _not_high(stock)


def _tail_entry_ok(stock: StockSnapshot) -> bool:
    return 0 < stock.change_pct <= 5.0 and _not_high(stock) and stock.amount > 0


def _not_high(stock: StockSnapshot) -> bool:
    return stock.change_pct <= 5.0


def _matched_positive_news(news_items: list[NewsItem], stock: StockSnapshot, board: BoardView) -> NewsItem | None:
    for item in news_items:
        if not item.url.strip():
            continue
        text = f"{item.title} {item.summary} {' '.join(item.related_names)}"
        if stock.name not in text and board.board.name not in text:
            continue
        if any(keyword in text for keyword in ("利好", "政策", "支持", "订单", "中标", "增长", "预增", "获批")):
            return item
    return None


def _positive_reason(item: NewsItem) -> str:
    text = f"{item.title} {item.summary}"
    reasons: list[str] = []
    if "政策" in text or "支持" in text:
        reasons.append("政策支持")
    if "订单" in text or "中标" in text:
        reasons.append("订单/中标")
    if "增长" in text or "预增" in text:
        reasons.append("增长预期")
    return "、".join(dict.fromkeys(reasons)) if reasons else "利好消息命中"


def _take_profit(entry: float) -> float:
    return entry * 1.06


def _overnight_take_profit(entry: float) -> float:
    return entry * 1.03


def _stop_loss(entry: float) -> float:
    return entry * 0.97
