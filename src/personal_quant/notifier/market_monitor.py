from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from personal_quant.data_center.models import MarketDataset, NewsItem, StockSnapshot
from personal_quant.data_center.positions import PositionSnapshot, load_positions
from personal_quant.data_center.providers import fetch_market_dataset
from personal_quant.market_analysis.classifier import BoardView, summarize_market
from personal_quant.notifier.feishu import FeishuPayload, FeishuWebhookResult, build_feishu_card_payload, send_feishu_payload
from personal_quant.notifier.webhook_env import resolve_feishu_webhook
from personal_quant.report_builder.run_audit import load_expected_dates
from personal_quant.watchlist_engine.selector import select_watchlist


NEGATIVE_NEWS_KEYWORDS = (
    "监管",
    "问询",
    "减持",
    "立案",
    "处罚",
    "暴雷",
    "澄清",
    "业绩下滑",
    "亏损",
    "预亏",
    "下修",
    "风险",
)
POSITIVE_NEWS_KEYWORDS = (
    "利好",
    "政策",
    "中标",
    "订单",
    "并购",
    "涨价",
    "预增",
    "业绩增长",
    "扩产",
)


@dataclass(frozen=True)
class MarketMonitorConfig:
    capital_outflow_threshold: float = -500000000.0
    capital_outflow_drop_pct: float = -1.0
    cooldown_minutes: int = 30
    negative_keywords: tuple[str, ...] = NEGATIVE_NEWS_KEYWORDS
    positive_keywords: tuple[str, ...] = POSITIVE_NEWS_KEYWORDS


@dataclass(frozen=True)
class MarketAlert:
    kind: str
    target: str
    severity: str
    action: str
    trigger: str
    evidence: str
    created_at: dt.datetime


@dataclass(frozen=True)
class MarketMonitorResult:
    now: dt.datetime
    dry_run: bool
    alerts: tuple[MarketAlert, ...]
    due_alerts: tuple[MarketAlert, ...]
    suppressed_alerts: tuple[MarketAlert, ...]
    responses: tuple[FeishuWebhookResult, ...]


Sender = Callable[[str, FeishuPayload], FeishuWebhookResult]
DataFetcher = Callable[[dt.date], MarketDataset]
NowFactory = Callable[[], dt.datetime]


def build_market_alerts(
    dataset: MarketDataset,
    positions: list[PositionSnapshot],
    now: dt.datetime,
    config: MarketMonitorConfig = MarketMonitorConfig(),
) -> tuple[MarketAlert, ...]:
    if not dataset.boards:
        raise ValueError("MARKET_MONITOR_BOARDS_EMPTY")
    if not dataset.stocks:
        raise ValueError("MARKET_MONITOR_STOCKS_EMPTY")
    _validate_config(config)
    summary = summarize_market(dataset)
    mainline = [item for item in summary.board_views if item.label == "主升候选"]
    watchlist = select_watchlist(summary, mainline)
    alerts: list[MarketAlert] = []
    alerts.extend(_negative_news_alerts(dataset.news_items, positions, mainline, watchlist, now, config))
    alerts.extend(_capital_outflow_alerts(summary.board_views, positions, now, config))
    alerts.extend(_positive_watchlist_alerts(dataset.news_items, watchlist, now, config))
    return tuple(_dedupe_alerts(alerts))


def run_market_monitor_once(
    output_dir: Path,
    state_path: Path,
    expected_dates_path: Path | None = Path("planning/runs/expected_dates.txt"),
    positions_path: Path | None = Path("data/positions.csv"),
    config: MarketMonitorConfig = MarketMonitorConfig(),
    live: bool = False,
    webhook_url: str | None = None,
    now: dt.datetime | None = None,
    data_fetcher: DataFetcher | None = None,
    sender: Sender = send_feishu_payload,
) -> Path:
    actual_now = now if now is not None else dt.datetime.now()
    as_of = actual_now.date()
    if expected_dates_path is not None:
        expected_dates = load_expected_dates(expected_dates_path)
        if as_of not in expected_dates:
            raise ValueError(f"MARKET_MONITOR_DATE_NOT_EXPECTED:{as_of.isoformat()}")
    positions = load_positions(positions_path) if positions_path is not None else []
    fetcher = data_fetcher if data_fetcher is not None else _fetch_market_monitor_dataset
    dataset = fetcher(as_of)
    alerts = build_market_alerts(dataset, positions, actual_now, config=config)
    state = _load_state(state_path)
    due_alerts, suppressed_alerts = _split_due_alerts(alerts, state, actual_now, config.cooldown_minutes)
    responses = _send_alerts(due_alerts, actual_now, live, webhook_url, sender)
    if live and due_alerts:
        _record_alerts(state_path, state, due_alerts, actual_now)
    result = MarketMonitorResult(
        now=actual_now,
        dry_run=not live,
        alerts=alerts,
        due_alerts=due_alerts,
        suppressed_alerts=suppressed_alerts,
        responses=tuple(responses),
    )
    return write_market_monitor_report(result, output_dir)


def run_market_monitor_loop(
    output_dir: Path,
    state_path: Path,
    expected_dates_path: Path | None = Path("planning/runs/expected_dates.txt"),
    positions_path: Path | None = Path("data/positions.csv"),
    config: MarketMonitorConfig = MarketMonitorConfig(),
    live: bool = False,
    webhook_url: str | None = None,
    interval_seconds: int = 60,
    market_start: dt.time = dt.time(9, 30),
    market_end: dt.time = dt.time(15, 0),
    now_factory: NowFactory = dt.datetime.now,
    data_fetcher: DataFetcher | None = None,
    sender: Sender = send_feishu_payload,
) -> Path:
    if interval_seconds <= 0:
        raise ValueError("MARKET_MONITOR_INTERVAL_INVALID")
    last_report: Path | None = None
    while True:
        current = now_factory()
        if current.time() > market_end:
            return _write_monitor_stopped_report(output_dir, current, last_report)
        if current.time() >= market_start:
            last_report = run_market_monitor_once(
                output_dir,
                state_path,
                expected_dates_path=expected_dates_path,
                positions_path=positions_path,
                config=config,
                live=live,
                webhook_url=webhook_url,
                now=current,
                data_fetcher=data_fetcher,
                sender=sender,
            )
        time.sleep(interval_seconds)


def write_market_monitor_report(result: MarketMonitorResult, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"market_monitor_{result.now.strftime('%Y%m%d_%H%M%S')}.md"
    output_path.write_text(build_market_monitor_report(result), encoding="utf-8")
    return output_path


def build_market_monitor_report(result: MarketMonitorResult) -> str:
    rows = [
        f"# 市场实时监控（{result.now.strftime('%Y-%m-%d %H:%M:%S')}）",
        "",
        "## 总判断",
        f"- 模式：{'dry-run' if result.dry_run else 'live'}",
        f"- 触发提醒：{len(result.due_alerts)}",
        f"- 冷却跳过：{len(result.suppressed_alerts)}",
        f"- 全部触发：{len(result.alerts)}",
        "",
        "## 触发清单",
        "| 对象 | 类型 | 级别 | 动作 | 触发条件 | 证据 |",
        "|---|---|---|---|---|---|",
    ]
    if not result.alerts:
        rows.append("| 暂无 | 暂无 | 暂无 | 观察 | 当前没有触发市场突发提醒 | 真实行情和新闻已检查 |")
    else:
        rows.extend(_alert_row(alert) for alert in result.alerts)
    if result.responses:
        rows.extend(["", "## 飞书响应", "| 序号 | HTTP | 响应 |", "|---:|---:|---|"])
        rows.extend(
            f"| {index} | {response.status_code} | {_table_text(response.response_text)} |"
            for index, response in enumerate(result.responses, start=1)
        )
    rows.extend(
        [
            "",
            "## 边界",
            "- 本监控只提醒，不下单。",
            "- 建仓观察必须有主升候选和结构化证据。",
        ]
    )
    return "\n".join(rows) + "\n"


def _negative_news_alerts(
    news_items: list[NewsItem],
    positions: list[PositionSnapshot],
    mainline: list[BoardView],
    watchlist: list[tuple[StockSnapshot, BoardView]],
    now: dt.datetime,
    config: MarketMonitorConfig,
) -> list[MarketAlert]:
    targets = _risk_targets(positions, mainline, watchlist)
    alerts: list[MarketAlert] = []
    for item in news_items:
        keywords = _matched_keywords(item, config.negative_keywords)
        if not keywords:
            continue
        for target in _matched_targets(item, targets):
            alerts.append(
                MarketAlert(
                    kind="negative_news",
                    target=target,
                    severity="高",
                    action="风险检查",
                    trigger=f"突发利空关键词：{'、'.join(keywords)}",
                    evidence=_news_evidence(item, target),
                    created_at=now,
                )
            )
    return alerts


def _capital_outflow_alerts(
    board_views: list[BoardView],
    positions: list[PositionSnapshot],
    now: dt.datetime,
    config: MarketMonitorConfig,
) -> list[MarketAlert]:
    held_names = "、".join(position.name for position in positions) if positions else "暂无"
    candidates = [
        item
        for item in board_views
        if item.board.main_net_inflow <= config.capital_outflow_threshold
        and (item.board.change_pct <= config.capital_outflow_drop_pct or item.label == "高位兑现")
    ]
    candidates.sort(key=lambda item: item.board.main_net_inflow)
    alerts: list[MarketAlert] = []
    for item in candidates[:3]:
        alerts.append(
            MarketAlert(
                kind="capital_outflow",
                target=item.board.name,
                severity="高",
                action="降低仓位观察",
                trigger=(
                    f"主力净流出 {item.board.main_net_inflow / 100000000:.2f}亿，"
                    f"板块涨跌 {item.board.change_pct:.2f}%"
                ),
                evidence=f"板块状态 {item.label}；{item.reason}；当前持仓：{held_names}",
                created_at=now,
            )
        )
    return alerts


def _positive_watchlist_alerts(
    news_items: list[NewsItem],
    watchlist: list[tuple[StockSnapshot, BoardView]],
    now: dt.datetime,
    config: MarketMonitorConfig,
) -> list[MarketAlert]:
    alerts: list[MarketAlert] = []
    for stock, board in watchlist:
        for item in news_items:
            keywords = _matched_keywords(item, config.positive_keywords)
            if not keywords:
                continue
            if not _news_matches(item, (stock.name, board.board.name)):
                continue
            alerts.append(
                MarketAlert(
                    kind="positive_watchlist",
                    target=f"{stock.name}({stock.code})",
                    severity="中",
                    action="建仓观察",
                    trigger=f"突发利好关键词：{'、'.join(keywords)}",
                    evidence=(
                        f"主升候选：{board.board.name}；候选股：{stock.name}；"
                        f"新闻：{item.title}；只提醒人工确认，不自动下单"
                    ),
                    created_at=now,
                )
            )
    return alerts


def _risk_targets(
    positions: list[PositionSnapshot],
    mainline: list[BoardView],
    watchlist: list[tuple[StockSnapshot, BoardView]],
) -> tuple[str, ...]:
    names: list[str] = []
    names.extend(position.name for position in positions)
    names.extend(item.board.name for item in mainline)
    names.extend(stock.name for stock, _board in watchlist)
    return tuple(dict.fromkeys(name for name in names if name))


def _matched_targets(item: NewsItem, targets: tuple[str, ...]) -> list[str]:
    return [target for target in targets if _news_matches(item, (target,))]


def _news_matches(item: NewsItem, targets: tuple[str, ...]) -> bool:
    text = _news_text(item)
    related = set(item.related_names)
    return any(target in text or target in related for target in targets)


def _matched_keywords(item: NewsItem, keywords: tuple[str, ...]) -> list[str]:
    text = _news_text(item)
    return [keyword for keyword in keywords if keyword in text]


def _news_evidence(item: NewsItem, target: str) -> str:
    return f"{item.published_at:%H:%M} {item.source} {item.title}；匹配对象：{target}"


def _news_text(item: NewsItem) -> str:
    return f"{item.title} {item.summary} {' '.join(item.related_names)}"


def _send_alerts(
    alerts: tuple[MarketAlert, ...],
    now: dt.datetime,
    live: bool,
    webhook_url: str | None,
    sender: Sender,
) -> list[FeishuWebhookResult]:
    if not live:
        return []
    resolved = resolve_feishu_webhook(webhook_url)
    if resolved.url is None:
        raise ValueError("FEISHU_WEBHOOK_URL_REQUIRED")
    return [sender(resolved.url, _format_alert_message(now, alert)) for alert in alerts]


def _format_alert_message(now: dt.datetime, alert: MarketAlert) -> FeishuPayload:
    return build_feishu_card_payload(
        title=f"个人量化｜市场实时提醒｜{alert.severity}级｜{alert.target}｜{now.strftime('%Y-%m-%d %H:%M:%S')}",
        template=_severity_template(alert.severity),
        markdown_blocks=(
            f"**对象**：{alert.target}",
            f"**下一步**：{alert.action}",
            f"**触发原因**：{alert.trigger}",
            f"**证据**：{alert.evidence}",
            "**边界**：本提醒只提示，不自动下单。",
        ),
    )


def _split_due_alerts(
    alerts: tuple[MarketAlert, ...],
    state: dict[str, str],
    now: dt.datetime,
    cooldown_minutes: int,
) -> tuple[tuple[MarketAlert, ...], tuple[MarketAlert, ...]]:
    due: list[MarketAlert] = []
    suppressed: list[MarketAlert] = []
    cooldown = dt.timedelta(minutes=cooldown_minutes)
    for alert in alerts:
        key = _alert_key(alert)
        sent_at_text = state.get(key)
        if sent_at_text is None:
            due.append(alert)
            continue
        sent_at = dt.datetime.fromisoformat(sent_at_text)
        if now - sent_at >= cooldown:
            due.append(alert)
        else:
            suppressed.append(alert)
    return tuple(due), tuple(suppressed)


def _load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"MARKET_MONITOR_STATE_INVALID:{path}")
    sent = data.get("sent", {})
    if not isinstance(sent, dict):
        raise ValueError(f"MARKET_MONITOR_STATE_INVALID:{path}")
    return {str(key): str(value) for key, value in sent.items()}


def _record_alerts(path: Path, state: dict[str, str], alerts: tuple[MarketAlert, ...], now: dt.datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    updated = dict(state)
    for alert in alerts:
        updated[_alert_key(alert)] = now.isoformat()
    path.write_text(json.dumps({"sent": updated}, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_monitor_stopped_report(output_dir: Path, now: dt.datetime, last_report: Path | None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"market_monitor_stopped_{now.strftime('%Y%m%d_%H%M%S')}.md"
    last_text = str(last_report).replace("\\", "/") if last_report is not None else "暂无"
    output_path.write_text(
        "\n".join(
            [
                f"# 市场实时监控停止（{now.strftime('%Y-%m-%d %H:%M:%S')}）",
                "",
                f"- 最后一次报告：{last_text}",
                "- 原因：已超过盘中监控结束时间。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return output_path


def _dedupe_alerts(alerts: list[MarketAlert]) -> list[MarketAlert]:
    seen: set[str] = set()
    result: list[MarketAlert] = []
    for alert in alerts:
        key = _alert_key(alert)
        if key in seen:
            continue
        seen.add(key)
        result.append(alert)
    return result


def _alert_key(alert: MarketAlert) -> str:
    return f"{alert.created_at.date().isoformat()}:{alert.kind}:{alert.target}"


def _fetch_market_monitor_dataset(as_of: dt.date) -> MarketDataset:
    return fetch_market_dataset(as_of, include_stocks=True, include_news=True)


def _validate_config(config: MarketMonitorConfig) -> None:
    if config.capital_outflow_threshold >= 0:
        raise ValueError("MARKET_MONITOR_OUTFLOW_THRESHOLD_INVALID")
    if config.capital_outflow_drop_pct >= 0:
        raise ValueError("MARKET_MONITOR_OUTFLOW_DROP_INVALID")
    if config.cooldown_minutes < 0:
        raise ValueError("MARKET_MONITOR_COOLDOWN_INVALID")


def _alert_row(alert: MarketAlert) -> str:
    return (
        f"| {alert.target} | {alert.kind} | {alert.severity} | {alert.action} | "
        f"{_table_text(alert.trigger)} | {_table_text(alert.evidence)} |"
    )


def _table_text(value: str) -> str:
    return " ".join(value.replace("|", "/").split())


def _severity_template(severity: str) -> str:
    if severity == "高":
        return "red"
    if severity == "中":
        return "orange"
    return "blue"
