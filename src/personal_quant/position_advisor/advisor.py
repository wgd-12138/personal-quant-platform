from __future__ import annotations

from dataclasses import dataclass

from personal_quant.data_center.models import StockSnapshot
from personal_quant.data_center.positions import PositionSnapshot


@dataclass(frozen=True)
class PositionAdvice:
    code: str
    name: str
    quantity: int
    cost_price: float
    current_price: float
    market_value: float
    pnl_pct: float
    board_label: str
    action: str
    stop_loss: str
    take_profit: str
    reduce_condition: str
    add_condition: str
    clear_condition: str


def advise_position(position: PositionSnapshot, stock: StockSnapshot, board_label: str) -> PositionAdvice:
    if stock.price <= 0:
        raise ValueError(f"持仓现价非法: {position.code}")
    pnl_pct = round((stock.price / position.cost_price - 1) * 100, 2)
    market_value = round(stock.price * position.quantity, 2)
    action = _action_for(pnl_pct, board_label)
    return PositionAdvice(
        code=position.code,
        name=position.name,
        quantity=position.quantity,
        cost_price=position.cost_price,
        current_price=stock.price,
        market_value=market_value,
        pnl_pct=pnl_pct,
        board_label=board_label,
        action=action,
        stop_loss=_stop_loss(position, stock, board_label),
        take_profit=_take_profit(stock, board_label),
        reduce_condition=_reduce_condition(board_label),
        add_condition=_add_condition(board_label),
        clear_condition=_clear_condition(board_label),
    )


def _action_for(pnl_pct: float, board_label: str) -> str:
    if board_label in {"高位兑现", "一日游风险"} and pnl_pct <= -5:
        return "清仓"
    if board_label in {"高位兑现", "一日游风险"}:
        return "减仓"
    if board_label == "主升候选" and pnl_pct >= -3:
        return "持有"
    if pnl_pct <= -7:
        return "减仓"
    return "观察"


def _stop_loss(position: PositionSnapshot, stock: StockSnapshot, board_label: str) -> str:
    if board_label in {"高位兑现", "一日游风险"}:
        return f"跌破 {position.cost_price * 0.95:.2f}，且板块仍为{board_label}"
    return f"跌破 {stock.price * 0.95:.2f}，或所属板块转为高位兑现"


def _take_profit(stock: StockSnapshot, board_label: str) -> str:
    if board_label == "主升候选":
        return f"冲高到 {stock.price * 1.10:.2f} 后放量滞涨，先减仓锁定利润"
    return f"反弹到 {stock.price * 1.06:.2f} 但成交不能继续放大，先减仓"


def _reduce_condition(board_label: str) -> str:
    if board_label == "主升候选":
        return "板块强度下降、个股放量滞涨或跌破 5 日趋势"
    return f"板块仍为{board_label}，盘中反抽缩量或个股放量下跌"


def _add_condition(board_label: str) -> str:
    if board_label == "主升候选":
        return "只在板块维持主升、个股回踩不破并重新放量时考虑补仓"
    return "不补仓，等待板块重新进入主升候选"


def _clear_condition(board_label: str) -> str:
    if board_label == "主升候选":
        return "跌破止损位、出现重大利空，或板块退出主升候选"
    return f"跌破止损位、出现重大利空，或板块继续处于{board_label}"
