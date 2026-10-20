from __future__ import annotations

from personal_quant.data_center.models import StockSnapshot
from personal_quant.market_analysis.classifier import BoardView, MarketSummary


def select_watchlist(summary: MarketSummary, mainline: list[BoardView]) -> list[tuple[StockSnapshot, BoardView]]:
    boards_by_name = {item.board.name: item for item in mainline}
    candidates = [
        (stock, boards_by_name[stock.industry])
        for stock in summary.dataset.stocks
        if stock.industry in boards_by_name and stock.change_pct > 0 and stock.amount > 0
    ]
    candidates.sort(key=lambda item: (item[0].change_pct, item[0].amount), reverse=True)
    return candidates[:10]
