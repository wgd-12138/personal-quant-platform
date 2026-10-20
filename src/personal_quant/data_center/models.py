from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BoardSnapshot:
    code: str
    name: str
    board_type: str
    change_pct: float
    amount: float
    main_net_inflow: float
    five_day_return: float
    sixty_day_return: float
    up_count: int
    down_count: int
    volume_ratio: float = 0.0
    twenty_day_return: float = 0.0


@dataclass(frozen=True)
class ExternalSnapshot:
    name: str
    symbol: str
    may_june_return: float
    july_return: float


@dataclass(frozen=True)
class IndexSnapshot:
    code: str
    name: str
    price: float
    change_pct: float
    change_amount: float
    amount: float


@dataclass(frozen=True)
class StockSnapshot:
    code: str
    name: str
    price: float
    change_pct: float
    amount: float
    turnover_rate: float
    industry: str
    high: float = 0.0
    low: float = 0.0
    previous_close: float = 0.0


@dataclass(frozen=True)
class NewsItem:
    source: str
    published_at: dt.datetime
    title: str
    summary: str
    related_names: tuple[str, ...]
    url: str


@dataclass(frozen=True)
class MarketDataset:
    as_of: dt.date
    boards: list[BoardSnapshot]
    externals: list[ExternalSnapshot]
    indexes: list[IndexSnapshot] = field(default_factory=list)
    stocks: list[StockSnapshot] = field(default_factory=list)
    news_items: list[NewsItem] = field(default_factory=list)
    data_sources: tuple[str, ...] = field(default_factory=tuple)
    fetched_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
