from __future__ import annotations

from personal_quant.data_center.models import NewsItem
from personal_quant.data_center.models import StockSnapshot
from personal_quant.data_center.positions import PositionSnapshot
from personal_quant.market_analysis.classifier import BoardView


def match_news(
    positions: list[PositionSnapshot],
    mainline: list[BoardView],
    news_items: list[NewsItem],
    watchlist_stocks: list[StockSnapshot] | None = None,
) -> list[tuple[NewsItem, list[str]]]:
    watchlist_stocks = watchlist_stocks or []
    targets = (
        [position.name for position in positions]
        + [item.board.name for item in mainline]
        + [stock.name for stock in watchlist_stocks]
    )
    matched: list[tuple[NewsItem, list[str]]] = []
    for item in news_items:
        text = f"{item.title} {item.summary}"
        hit = sorted({target for target in targets if target and target in text})
        if hit:
            matched.append((item, hit))
    return matched
