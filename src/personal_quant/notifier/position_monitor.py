from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from personal_quant.data_center.models import MarketDataset, StockSnapshot
from personal_quant.data_center.positions import PositionSnapshot, load_positions
from personal_quant.data_center.providers import fetch_market_dataset
from personal_quant.market_analysis.classifier import MarketSummary, summarize_market
from personal_quant.notifier.feishu import FeishuPayload, FeishuWebhookResult, build_feishu_card_payload, send_feishu_payload
from personal_quant.notifier.webhook_env import resolve_feishu_webhook
from personal_quant.position_advisor.advisor import PositionAdvice, advise_position
from personal_quant.report_builder.run_audit import load_expected_dates


@dataclass(frozen=True)
class PositionMonitorConfig:
    stop_loss_pct: float = -5.0
    take_profit_pct: float = 10.0
    intraday_drop_pct: float = -5.0
    cooldown_minutes: int = 30


@dataclass(frozen=True)
class PositionAlert:
    code: str
    name: str
    kind: str
    severity: str
    action: str
    trigger: str
    evidence: str
    current_price: float
    cost_price: float
    pnl_pct: float
    change_pct: float
    board_label: str
    created_at: dt.datetime


@dataclass(frozen=True)
class PositionMonitorResult:
    now: dt.datetime
    dry_run: bool
    alerts: tuple[PositionAlert, ...]
    due_alerts: tuple[PositionAlert, ...]
    suppressed_alerts: tuple[PositionAlert, ...]
    responses: tuple[FeishuWebhookResult, ...]


Sender = Callable[[str, FeishuPayload], FeishuWebhookResult]
DataFetcher = Callable[[dt.date], MarketDataset]
NowFactory = Callable[[], dt.datetime]


def build_position_alerts(
    positions: list[PositionSnapshot],
    dataset: MarketDataset,
    now: dt.datetime,
    config: PositionMonitorConfig = PositionMonitorConfig(),
) -> tuple[PositionAlert, ...]:
    if not positions:
        raise ValueError("POSITION_MONITOR_POSITIONS_EMPTY")
    if not dataset.stocks:
        raise ValueError("POSITION_MONITOR_STOCKS_EMPTY")
    _validate_config(config)
    summary = summarize_market(dataset)
    alerts: list[PositionAlert] = []
    for position in positions:
        stock = _stock_for_position(position, dataset.stocks)
        board_label = _board_label_for(summary, stock)
        advice = advise_position(position, stock, board_label)
        alerts.extend(_alerts_for_advice(advice, stock, now, config))
    return tuple(alerts)


def run_position_monitor_once(
    positions_path: Path,
    output_dir: Path,
    state_path: Path,
    expected_dates_path: Path | None = None,
    config: PositionMonitorConfig = PositionMonitorConfig(),
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
            raise ValueError(f"POSITION_MONITOR_DATE_NOT_EXPECTED:{as_of.isoformat()}")
    positions = load_positions(positions_path)
    fetcher = data_fetcher if data_fetcher is not None else _fetch_position_dataset
    dataset = fetcher(as_of)
    alerts = build_position_alerts(positions, dataset, actual_now, config=config)
    state = _load_state(state_path)
    due_alerts, suppressed_alerts = _split_due_alerts(alerts, state, actual_now, config.cooldown_minutes)
    responses = _send_alerts(due_alerts, actual_now, live, webhook_url, sender)
    if live and due_alerts:
        _record_alerts(state_path, state, due_alerts, actual_now)
    result = PositionMonitorResult(
        now=actual_now,
        dry_run=not live,
        alerts=alerts,
        due_alerts=due_alerts,
        suppressed_alerts=suppressed_alerts,
        responses=tuple(responses),
    )
    return write_position_monitor_report(result, output_dir)


def run_position_monitor_loop(
    positions_path: Path,
    output_dir: Path,
    state_path: Path,
    expected_dates_path: Path | None = None,
    config: PositionMonitorConfig = PositionMonitorConfig(),
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
        raise ValueError("POSITION_MONITOR_INTERVAL_INVALID")
    last_report: Path | None = None
    while True:
        current = now_factory()
        if current.time() > market_end:
            return _write_monitor_stopped_report(output_dir, current, last_report)
        if current.time() >= market_start:
            last_report = run_position_monitor_once(
                positions_path,
                output_dir,
                state_path,
                expected_dates_path=expected_dates_path,
                config=config,
                live=live,
                webhook_url=webhook_url,
                now=current,
                data_fetcher=data_fetcher,
                sender=sender,
            )
        time.sleep(interval_seconds)


def write_position_monitor_report(result: PositionMonitorResult, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"position_monitor_{result.now.strftime('%Y%m%d_%H%M%S')}.md"
    output_path.write_text(build_position_monitor_report(result), encoding="utf-8")
    return output_path


def build_position_monitor_report(result: PositionMonitorResult) -> str:
    rows = [
        f"# 持仓实时监控（{result.now.strftime('%Y-%m-%d %H:%M:%S')}）",
        "",
        "## 总判断",
        f"- 模式：{'dry-run' if result.dry_run else 'live'}",
        f"- 触发提醒：{len(result.due_alerts)}",
        f"- 冷却跳过：{len(result.suppressed_alerts)}",
        f"- 全部触发：{len(result.alerts)}",
        "",
        "## 触发清单",
        "| 股票 | 类型 | 级别 | 操作 | 触发条件 | 证据 |",
        "|---|---|---|---|---|---|",
    ]
    if not result.alerts:
        rows.append("| 暂无 | 暂无 | 暂无 | 观察 | 当前没有触发持仓提醒 | 真实行情已检查 |")
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
            "- 缺真实持仓、缺真实行情或交易日不在清单时直接失败。",
        ]
    )
    return "\n".join(rows) + "\n"


def _alerts_for_advice(
    advice: PositionAdvice,
    stock: StockSnapshot,
    now: dt.datetime,
    config: PositionMonitorConfig,
) -> list[PositionAlert]:
    alerts: list[PositionAlert] = []
    if advice.pnl_pct <= config.stop_loss_pct:
        alerts.append(
            _alert(
                advice,
                stock,
                now,
                kind="stop_loss",
                severity="高",
                action="止损检查" if advice.action not in {"减仓", "清仓"} else advice.action,
                trigger=f"浮盈亏 {advice.pnl_pct:.2f}% <= {config.stop_loss_pct:.2f}%",
                evidence=advice.stop_loss,
            )
        )
    if advice.pnl_pct >= config.take_profit_pct:
        alerts.append(
            _alert(
                advice,
                stock,
                now,
                kind="take_profit",
                severity="中",
                action="止盈检查",
                trigger=f"浮盈亏 {advice.pnl_pct:.2f}% >= {config.take_profit_pct:.2f}%",
                evidence=advice.take_profit,
            )
        )
    if stock.change_pct <= config.intraday_drop_pct:
        alerts.append(
            _alert(
                advice,
                stock,
                now,
                kind="intraday_drop",
                severity="中",
                action="盘中下跌检查",
                trigger=f"日内涨跌幅 {stock.change_pct:.2f}% <= {config.intraday_drop_pct:.2f}%",
                evidence=advice.reduce_condition,
            )
        )
    if advice.action in {"减仓", "清仓"}:
        alerts.append(
            _alert(
                advice,
                stock,
                now,
                kind=f"advice_{advice.action}",
                severity="高" if advice.action == "清仓" else "中",
                action=advice.action,
                trigger=f"持仓建议为{advice.action}，板块状态 {advice.board_label}",
                evidence=advice.clear_condition if advice.action == "清仓" else advice.reduce_condition,
            )
        )
    return _dedupe_alerts(alerts)


def _alert(
    advice: PositionAdvice,
    stock: StockSnapshot,
    now: dt.datetime,
    kind: str,
    severity: str,
    action: str,
    trigger: str,
    evidence: str,
) -> PositionAlert:
    return PositionAlert(
        code=advice.code,
        name=advice.name,
        kind=kind,
        severity=severity,
        action=action,
        trigger=trigger,
        evidence=evidence,
        current_price=advice.current_price,
        cost_price=advice.cost_price,
        pnl_pct=advice.pnl_pct,
        change_pct=stock.change_pct,
        board_label=advice.board_label,
        created_at=now,
    )


def _send_alerts(
    alerts: tuple[PositionAlert, ...],
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


def _format_alert_message(now: dt.datetime, alert: PositionAlert) -> FeishuPayload:
    return build_feishu_card_payload(
        title=f"个人量化｜持仓实时提醒｜{alert.severity}级｜{alert.name}｜{now.strftime('%Y-%m-%d %H:%M:%S')}",
        template=_severity_template(alert.severity),
        markdown_blocks=(
            f"**下一步**：{alert.action}",
            f"**股票**：{alert.name}({alert.code})",
            f"**触发原因**：{alert.trigger}",
            (
                f"**关键数据**：现价 {alert.current_price:.2f}，成本 {alert.cost_price:.2f}，"
                f"浮盈亏 {alert.pnl_pct:.2f}%，日内 {alert.change_pct:.2f}%，板块 {alert.board_label}"
            ),
            f"**处理纪律**：{alert.evidence}",
            "**边界**：本提醒只提示，不自动下单。",
        ),
    )


def _stock_for_position(position: PositionSnapshot, stocks: list[StockSnapshot]) -> StockSnapshot:
    stock_by_code = {_code_key(stock.code): stock for stock in stocks}
    stock = stock_by_code.get(_code_key(position.code))
    if stock is None:
        raise ValueError(f"POSITION_MARKET_DATA_MISSING:{position.code}")
    return stock


def _board_label_for(summary: MarketSummary, stock: StockSnapshot) -> str:
    for item in summary.board_views:
        if item.board.name == stock.industry:
            return item.label
    raise ValueError(f"POSITION_BOARD_DATA_MISSING:{stock.code}:{stock.industry}")


def _split_due_alerts(
    alerts: tuple[PositionAlert, ...],
    state: dict[str, str],
    now: dt.datetime,
    cooldown_minutes: int,
) -> tuple[tuple[PositionAlert, ...], tuple[PositionAlert, ...]]:
    due: list[PositionAlert] = []
    suppressed: list[PositionAlert] = []
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
        raise ValueError(f"POSITION_MONITOR_STATE_INVALID:{path}")
    sent = data.get("sent", {})
    if not isinstance(sent, dict):
        raise ValueError(f"POSITION_MONITOR_STATE_INVALID:{path}")
    return {str(key): str(value) for key, value in sent.items()}


def _record_alerts(path: Path, state: dict[str, str], alerts: tuple[PositionAlert, ...], now: dt.datetime) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    updated = dict(state)
    for alert in alerts:
        updated[_alert_key(alert)] = now.isoformat()
    path.write_text(json.dumps({"sent": updated}, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_monitor_stopped_report(output_dir: Path, now: dt.datetime, last_report: Path | None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"position_monitor_stopped_{now.strftime('%Y%m%d_%H%M%S')}.md"
    last_text = str(last_report).replace("\\", "/") if last_report is not None else "暂无"
    output_path.write_text(
        "\n".join(
            [
                f"# 持仓实时监控停止（{now.strftime('%Y-%m-%d %H:%M:%S')}）",
                "",
                f"- 最后一次报告：{last_text}",
                "- 原因：已超过盘中监控结束时间。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return output_path


def _dedupe_alerts(alerts: list[PositionAlert]) -> list[PositionAlert]:
    seen: set[str] = set()
    result: list[PositionAlert] = []
    for alert in alerts:
        key = f"{alert.code}:{alert.kind}"
        if key in seen:
            continue
        seen.add(key)
        result.append(alert)
    return result


def _alert_key(alert: PositionAlert) -> str:
    return f"{alert.created_at.date().isoformat()}:{alert.code}:{alert.kind}"


def _fetch_position_dataset(as_of: dt.date) -> MarketDataset:
    return fetch_market_dataset(as_of, include_stocks=True, include_news=False)


def _validate_config(config: PositionMonitorConfig) -> None:
    if config.stop_loss_pct >= 0:
        raise ValueError("POSITION_MONITOR_STOP_LOSS_INVALID")
    if config.take_profit_pct <= 0:
        raise ValueError("POSITION_MONITOR_TAKE_PROFIT_INVALID")
    if config.intraday_drop_pct >= 0:
        raise ValueError("POSITION_MONITOR_INTRADAY_DROP_INVALID")
    if config.cooldown_minutes < 0:
        raise ValueError("POSITION_MONITOR_COOLDOWN_INVALID")


def _code_key(value: str) -> str:
    stripped = value.strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if stripped.startswith(prefix):
            return stripped.removeprefix(prefix)
    return stripped


def _alert_row(alert: PositionAlert) -> str:
    return (
        f"| {alert.name}({alert.code}) | {alert.kind} | {alert.severity} | {alert.action} | "
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
