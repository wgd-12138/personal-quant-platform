from __future__ import annotations

from dataclasses import dataclass

from personal_quant.data_center.models import BoardSnapshot, MarketDataset


@dataclass(frozen=True)
class BoardView:
    board: BoardSnapshot
    label: str
    reason: str
    score: float


@dataclass(frozen=True)
class MarketSummary:
    dataset: MarketDataset
    board_views: list[BoardView]
    market_state: str
    operation_advice: str


def classify_board(board: BoardSnapshot) -> BoardView:
    if board.sixty_day_return >= 35 and board.five_day_return <= 15:
        label = "高位兑现"
        reason = "前期涨幅过大，短期强度不足"
    elif board.sixty_day_return < -5 and 2 <= board.five_day_return < 10 and board.up_count >= 10:
        label = "反弹候选"
        reason = "前期下跌后修复，暂不按主升处理"
    elif board.five_day_return >= 10 and board.main_net_inflow > 0 and board.up_count >= 5:
        label = "主升候选"
        reason = "短期强度高，资金为正，板块有扩散"
    elif board.change_pct >= 4 and board.five_day_return < 3:
        label = "一日游风险"
        reason = "当日强但周期强度不足"
    else:
        label = "观察候选"
        reason = "强度或持续性不足"
    return BoardView(board=board, label=label, reason=reason, score=score_board(board, label))


def score_board(board: BoardSnapshot, label: str) -> float:
    score = board.five_day_return * 2 + min(board.up_count, 30) * 0.8
    if board.main_net_inflow > 0:
        score += 5
    if label == "主升候选":
        score += 20
    if label == "高位兑现":
        score -= 20
    if label == "反弹候选":
        score -= 8
    return round(score, 2)


def summarize_market(dataset: MarketDataset) -> MarketSummary:
    if len(dataset.boards) == 0:
        raise ValueError("板块数据为空")
    if len(dataset.externals) == 0:
        raise ValueError("外围数据为空")
    board_views = [classify_board(board) for board in dataset.boards]
    board_views.sort(key=lambda item: item.score, reverse=True)
    mainline_count = sum(1 for item in board_views if item.label == "主升候选")
    rebound_count = sum(1 for item in board_views if item.label == "反弹候选")
    cashout_count = sum(1 for item in board_views if item.label == "高位兑现")
    if mainline_count > 0:
        market_state = "出现主升候选"
        operation_advice = "只围绕主升候选观察，不追一日游"
    elif rebound_count > 0 and cashout_count > 0:
        market_state = "高位兑现 + 低位反弹"
        operation_advice = "防守为主，反弹只观察，不重仓试错"
    elif rebound_count > 0:
        market_state = "低位反弹"
        operation_advice = "轻仓观察，等待持续性确认"
    else:
        market_state = "无清晰主线"
        operation_advice = "空仓或低仓位观察"
    return MarketSummary(
        dataset=dataset,
        board_views=board_views,
        market_state=market_state,
        operation_advice=operation_advice,
    )
