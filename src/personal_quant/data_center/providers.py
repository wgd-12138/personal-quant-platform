from __future__ import annotations

import datetime as dt
import json
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from personal_quant.data_center.models import BoardSnapshot, ExternalSnapshot, IndexSnapshot, MarketDataset, NewsItem, StockSnapshot


EASTMONEY_UT = "fa5fd1943c7b386f172d6893dbfba10b"

EXTERNAL_SYMBOLS = {
    "纳斯达克": ".IXIC",
    "费城半导体ETF": "SOXX",
    "英伟达": "NVDA",
    "AMD": "AMD",
    "博通": "AVGO",
    "美光": "MU",
    "台积电": "TSM",
}


def fetch_market_dataset(as_of: dt.date, include_stocks: bool = False, include_news: bool = False) -> MarketDataset:
    boards = fetch_board_snapshots("industry") + fetch_board_snapshots("concept")
    externals = fetch_external_snapshots(as_of)
    indexes = fetch_index_snapshots()
    stocks = fetch_stock_snapshots() if include_stocks else []
    news_items = fetch_news_items(as_of) if include_news else []
    validate_dataset(boards, externals)
    validate_index_snapshots(indexes)
    if include_stocks:
        validate_stock_snapshots(stocks)
    data_sources = ["东方财富板块", "东方财富指数", "新浪财经美股历史"]
    if include_stocks:
        data_sources.append("东方财富个股")
    if include_news:
        data_sources.append("东方财富快讯")
    return MarketDataset(
        as_of=as_of,
        boards=boards,
        externals=externals,
        indexes=indexes,
        stocks=stocks,
        news_items=news_items,
        data_sources=tuple(data_sources),
        fetched_at=dt.datetime.now(dt.UTC),
    )


def fetch_board_snapshots(board_type: str) -> list[BoardSnapshot]:
    if board_type not in {"industry", "concept"}:
        raise ValueError("board_type must be industry or concept")
    fs = "m:90+t:2+f:!50" if board_type == "industry" else "m:90+t:3+f:!50"
    rows: list[dict[str, object]] = []
    page = 1
    page_size = 100
    total = None
    while total is None or len(rows) < total:
        payload = get_json(
            "https://push2.eastmoney.com/webguest/api/qt/clist/get",
            {
                "pn": page,
                "pz": page_size,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": fs,
                "fields": "f12,f14,f3,f6,f10,f24,f62,f104,f105,f109,f110",
                "ut": EASTMONEY_UT,
                "dect": 1,
                "wbp2u": "|0|0|0|web",
            },
        )
        data = payload.get("data") or {}
        total = int(data.get("total") or 0)
        page_rows = data.get("diff") or []
        if not page_rows:
            break
        rows.extend(page_rows)
        page += 1
    snapshots = [_parse_board_row(row, board_type) for row in rows]
    return [item for item in snapshots if item is not None]


def fetch_external_snapshots(as_of: dt.date, fetch_bars=None) -> list[ExternalSnapshot]:
    fetch_bars = fetch_bars or fetch_sina_us_bars
    start = dt.date(as_of.year, 5, 1)
    may_june_end = dt.date(as_of.year, 6, 30)
    july_start = dt.date(as_of.year, 7, 1)
    externals: list[ExternalSnapshot] = []
    for name, symbol in EXTERNAL_SYMBOLS.items():
        bars = fetch_bars(symbol, start, as_of)
        may_june = period_return(bars, start, may_june_end)
        july = period_return(bars, july_start, as_of)
        if may_june is None or july is None:
            raise ValueError(f"外围数据不完整: {name}")
        externals.append(
            ExternalSnapshot(
                name=name,
                symbol=symbol,
                may_june_return=may_june,
                july_return=july,
            )
        )
    return externals


def fetch_index_snapshots() -> list[IndexSnapshot]:
    payload = get_json(
        "https://push2.eastmoney.com/webguest/api/qt/ulist.np/get",
        {
            "fltt": 2,
            "invt": 2,
            "fields": "f12,f14,f2,f3,f4,f6",
            "secids": "1.000001,0.399001,0.399006,1.000688,0.899050",
            "ut": EASTMONEY_UT,
        },
    )
    data = payload.get("data") or {}
    rows = data.get("diff") or []
    snapshots = [_parse_index_row(row) for row in rows]
    return [item for item in snapshots if item is not None]


def fetch_stock_snapshots() -> list[StockSnapshot]:
    rows: list[dict[str, object]] = []
    page = 1
    page_size = 200
    total = None
    while total is None or len(rows) < total:
        payload = get_json(
            "https://push2.eastmoney.com/webguest/api/qt/clist/get",
            {
                "pn": page,
                "pz": page_size,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f12,f14,f2,f3,f6,f8,f15,f16,f18,f100",
                "ut": EASTMONEY_UT,
            },
        )
        data = payload.get("data") or {}
        total = int(data.get("total") or 0)
        page_rows = data.get("diff") or []
        if not page_rows:
            break
        rows.extend(page_rows)
        page += 1
    snapshots = [_parse_stock_row(row) for row in rows]
    return [item for item in snapshots if item is not None]


def fetch_news_items(as_of: dt.date, page_size: int = 50) -> list[NewsItem]:
    sort_end = dt.datetime.combine(as_of, dt.time(23, 59, 59)).strftime("%Y-%m-%d %H:%M:%S")
    payload = get_json(
        "https://np-listapi.eastmoney.com/comm/web/getFastNewsList",
        {
            "client": "web",
            "biz": "web_724",
            "fastColumn": "102",
            "pageSize": page_size,
            "sortEnd": sort_end,
            "req_trace": f"daily-pack-{as_of.isoformat()}",
        },
    )
    data = payload.get("data") or {}
    rows = data.get("fastNewsList") or []
    items = [_parse_news_row(row) for row in rows]
    return [item for item in items if item is not None]


def fetch_yahoo_bars(symbol: str, start: dt.date, end: dt.date) -> list[dict[str, object]]:
    start_ts = int(dt.datetime.combine(start, dt.time.min, tzinfo=dt.UTC).timestamp())
    end_ts = int(dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC).timestamp())
    payload = get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        {"period1": start_ts, "period2": end_ts, "interval": "1d"},
    )
    result = (payload.get("chart", {}).get("result") or [None])[0]
    if not result:
        return []
    timestamps = result.get("timestamp") or []
    closes = ((result.get("indicators", {}).get("quote") or [{}])[0]).get("close") or []
    bars: list[dict[str, object]] = []
    for timestamp, close in zip(timestamps, closes):
        if close is None:
            continue
        bars.append({"date": dt.datetime.fromtimestamp(timestamp, tz=dt.UTC).date(), "close": float(close)})
    return bars


def fetch_sina_us_bars(symbol: str, start: dt.date, end: dt.date) -> list[dict[str, object]]:
    text = get_text(
        "https://stock.finance.sina.com.cn/usstock/api/json_v2.php/US_MinKService.getDailyK",
        {"symbol": symbol},
    )
    bars = _parse_sina_us_bars(text)
    return [bar for bar in bars if start <= bar["date"] <= end or bar["date"] < start]


def get_text(url: str, params: dict[str, object], retries: int = 3) -> str:
    request = Request(
        f"{url}?{urlencode(params)}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=20) as response:
                return response.read().decode("utf-8")
        except Exception as exc:
            last_error = exc
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"数据源请求失败: {url}") from last_error


def validate_dataset(boards: list[BoardSnapshot], externals: list[ExternalSnapshot]) -> None:
    if len(boards) < 300:
        raise ValueError(f"板块数据不完整: {len(boards)}")
    if len(externals) < 5:
        raise ValueError(f"外围数据不完整: {len(externals)}")


def validate_index_snapshots(indexes: list[IndexSnapshot]) -> None:
    if len(indexes) < 5:
        raise ValueError(f"指数数据不完整: {len(indexes)}")


def validate_stock_snapshots(stocks: list[StockSnapshot]) -> None:
    if len(stocks) < 4000:
        raise ValueError(f"个股数据不完整: {len(stocks)}")


def get_json(url: str, params: dict[str, object], retries: int = 3) -> dict[str, object]:
    request = Request(
        f"{url}?{urlencode(params)}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/",
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(request, timeout=20) as response:
                text = response.read().decode("utf-8")
            data = json.loads(text)
            if data.get("rc") not in (0, None):
                raise RuntimeError(f"接口返回错误: {data}")
            if "code" in data and str(data.get("code")) == "0" and data.get("message") != "success":
                raise RuntimeError(f"接口返回错误: {data}")
            return data
        except Exception as exc:
            last_error = exc
            time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"数据源请求失败: {url}") from last_error


def period_return(bars: list[dict[str, object]], start: dt.date, end: dt.date) -> float | None:
    selected = [bar for bar in bars if start <= bar["date"] <= end]
    if not selected:
        return None
    previous = [bar for bar in bars if bar["date"] < start]
    base_bar = previous[-1] if previous else selected[0]
    if base_bar is selected[-1]:
        return None
    first = float(base_bar["close"])
    last = float(selected[-1]["close"])
    if first <= 0:
        return None
    return round((last / first - 1) * 100, 2)


def _parse_board_row(row: dict[str, object], board_type: str) -> BoardSnapshot | None:
    required = ["f12", "f14", "f3", "f6", "f10", "f24", "f62", "f104", "f105", "f109", "f110"]
    if any(row.get(key) in (None, "-") for key in required):
        return None
    return BoardSnapshot(
        code=str(row["f12"]),
        name=str(row["f14"]),
        board_type="行业" if board_type == "industry" else "概念",
        change_pct=float(row["f3"]),
        amount=float(row["f6"]),
        main_net_inflow=float(row["f62"]),
        five_day_return=float(row["f109"]),
        sixty_day_return=float(row["f24"]),
        up_count=int(row["f104"]),
        down_count=int(row["f105"]),
        volume_ratio=float(row["f10"]),
        twenty_day_return=float(row["f110"]),
    )


def _parse_index_row(row: dict[str, object]) -> IndexSnapshot | None:
    required = ["f12", "f14", "f2", "f3", "f4", "f6"]
    if any(row.get(key) in (None, "-") for key in required):
        return None
    return IndexSnapshot(
        code=str(row["f12"]),
        name=str(row["f14"]),
        price=float(row["f2"]),
        change_pct=float(row["f3"]),
        change_amount=float(row["f4"]),
        amount=float(row["f6"]),
    )


def _parse_stock_row(row: dict[str, object]) -> StockSnapshot | None:
    required = ["f12", "f14", "f2", "f3", "f6", "f8", "f15", "f16", "f18", "f100"]
    if any(row.get(key) in (None, "-") for key in required):
        return None
    return StockSnapshot(
        code=str(row["f12"]),
        name=str(row["f14"]),
        price=float(row["f2"]),
        change_pct=float(row["f3"]),
        amount=float(row["f6"]),
        turnover_rate=float(row["f8"]),
        industry=str(row["f100"]),
        high=float(row["f15"]),
        low=float(row["f16"]),
        previous_close=float(row["f18"]),
    )


def _parse_sina_us_bars(payload: str) -> list[dict[str, object]]:
    rows = json.loads(payload)
    if not isinstance(rows, list):
        raise ValueError("新浪外围数据格式非法")
    bars: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("d") or not row.get("c"):
            continue
        try:
            bars.append(
                {
                    "date": dt.date.fromisoformat(str(row["d"])),
                    "close": float(row["c"]),
                }
            )
        except ValueError:
            continue
    if not bars:
        raise ValueError("新浪外围数据为空")
    return bars


def _parse_news_row(row: dict[str, object]) -> NewsItem | None:
    title = str(row.get("title") or "").strip()
    summary = str(row.get("summary") or "").strip()
    show_time = str(row.get("showTime") or "").strip()
    if not title or not summary or not show_time:
        return None
    try:
        published_at = dt.datetime.strptime(show_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    related = tuple(str(item) for item in (row.get("stockList") or []) if item)
    code = str(row.get("code") or "").strip()
    url = f"https://finance.eastmoney.com/a/{code}.html" if code else ""
    return NewsItem(
        source="东方财富快讯",
        published_at=published_at,
        title=title,
        summary=summary,
        related_names=related,
        url=url,
    )
