from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from personal_quant.notifier.feishu import FeishuPayload, FeishuWebhookResult, build_feishu_card_payload, send_feishu_payload
from personal_quant.notifier.schedule import PushSlot, get_push_slot
from personal_quant.notifier.webhook_env import resolve_feishu_webhook
from personal_quant.report_builder.archive import report_archive_dir
from personal_quant.report_builder.push_preview import PUSH_MESSAGES


@dataclass(frozen=True)
class PushMessage:
    key: str
    title: str
    text: str


@dataclass(frozen=True)
class PushSendResult:
    as_of: dt.date
    slot: PushSlot
    dry_run: bool
    messages: tuple[PushMessage, ...]
    responses: tuple[FeishuWebhookResult, ...]


Sender = Callable[[str, FeishuPayload], FeishuWebhookResult]


EVENING_TITLE_OVERRIDES = {
    "intraday": "今日盘面总结",
    "positions": "个股操作与仓位调整",
    "news": "利好利空消息分析",
}

MESSAGE_CARD_TEMPLATES = {
    "morning_build": "green",
    "intraday": "blue",
    "positions": "orange",
    "watchlist": "green",
    "news": "purple",
    "overnight_arbitrage": "orange",
    "evening_review": "orange",
    "next_day": "blue",
    "risk": "red",
}

FOCUS_PREFIXES = {
    "intraday": ("- 盘面：", "- 主线：", "- 结论：", "- 策略：", "- 资金："),
    "morning_build": ("暂无适合今日建仓", "- "),
    "positions": ("- 总体：", "- 分组："),
    "watchlist": ("暂无满足主升条件", "- 候选：", "- 建仓："),
    "news": ("暂无匹配消息", "- 利好：", "- 利空：", "- 消息："),
    "overnight_arbitrage": ("暂无满足隔夜套利", "- "),
    "evening_review": ("- 复盘：",),
    "next_day": ("- 次日：", "- 关注："),
    "risk": ("- 风险：",),
}

HIDDEN_SECTION_TITLES = {"数据质量"}


def load_slot_messages(as_of: dt.date, reports_dir: Path, slot_key: str) -> tuple[PushMessage, ...]:
    slot = get_push_slot(slot_key)
    preview_path = report_archive_dir(reports_dir, as_of) / f"push_preview_{as_of.isoformat()}.md"
    text = _read_required(preview_path)
    sections = _parse_push_preview_sections(text)
    messages: list[PushMessage] = []
    titles = {key: title for key, title, _trigger in PUSH_MESSAGES}
    for key in slot.message_keys:
        if key not in sections:
            raise ValueError(f"PUSH_PREVIEW_MESSAGE_MISSING:{key}")
        content = sections[key].strip()
        if not content:
            raise ValueError(f"PUSH_PREVIEW_MESSAGE_EMPTY:{key}")
        messages.append(PushMessage(key=key, title=titles[key], text=content))
    return tuple(messages)


def send_push_slot(
    as_of: dt.date,
    reports_dir: Path,
    slot_key: str,
    output_dir: Path,
    live: bool = False,
    webhook_url: str | None = None,
    sender: Sender = send_feishu_payload,
) -> Path:
    slot = get_push_slot(slot_key)
    messages = load_slot_messages(as_of, reports_dir, slot.key)
    responses: list[FeishuWebhookResult] = []
    if live:
        resolved = resolve_feishu_webhook(webhook_url)
        if resolved.url is None:
            raise ValueError("FEISHU_WEBHOOK_URL_REQUIRED")
        for message in messages:
            responses.append(sender(resolved.url, _build_feishu_message_payload(as_of, slot, message)))
    result = PushSendResult(
        as_of=as_of,
        slot=slot,
        dry_run=not live,
        messages=messages,
        responses=tuple(responses),
    )
    return write_push_receipt_report(result, output_dir)


def write_push_receipt_report(result: PushSendResult, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"push_receipt_{result.as_of.isoformat()}_{result.slot.key}.md"
    output_path.write_text(build_push_receipt_report(result), encoding="utf-8")
    return output_path


def build_push_receipt_report(result: PushSendResult) -> str:
    rows = [
        f"# 推送回执（{result.as_of.isoformat()} {result.slot.name}）",
        "",
        "## 总判断",
        f"- 模式：{'dry-run' if result.dry_run else 'live'}",
        f"- 通道：飞书 webhook",
        f"- 消息数：{len(result.messages)}",
        f"- 发送状态：{'未发送，仅检查内容' if result.dry_run else '已发送'}",
        "",
        "## 消息清单",
        "| 序号 | 内容 | 字数 |",
        "|---:|---|---:|",
    ]
    rows.extend(
        f"| {index} | {_display_title(result.slot, message)} | "
        f"{_payload_size(_build_feishu_message_payload(result.as_of, result.slot, message))} |"
        for index, message in enumerate(result.messages, start=1)
    )
    if result.responses:
        rows.extend(["", "## 飞书响应", "| 序号 | HTTP | 响应 |", "|---:|---:|---|"])
        rows.extend(
            f"| {index} | {response.status_code} | {_escape_table(response.response_text)} |"
            for index, response in enumerate(result.responses, start=1)
        )
    return "\n".join(rows) + "\n"


def _build_feishu_message_payload(as_of: dt.date, slot: PushSlot, message: PushMessage) -> FeishuPayload:
    title = f"个人量化｜{slot.name}｜{_display_title(slot, message)}｜{as_of.isoformat()} {slot.time}"
    return build_feishu_card_payload(
        title=title,
        template=MESSAGE_CARD_TEMPLATES[message.key],
        markdown_blocks=_human_markdown_blocks(message)
        + ("**边界**\n只提示，不自动下单；结论来自本地报告。",),
    )


def _display_title(slot: PushSlot, message: PushMessage) -> str:
    if slot.key == "evening":
        return EVENING_TITLE_OVERRIDES.get(message.key, message.title)
    return message.title


def _parse_push_preview_sections(text: str) -> dict[str, str]:
    headings = {title: key for key, title, _trigger in PUSH_MESSAGES}
    current_key: str | None = None
    current_lines: list[str] = []
    sections: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("## "):
            title = line.removeprefix("## ").strip()
            next_key = headings.get(title)
            if next_key is not None:
                if current_key is not None:
                    sections[current_key] = "\n".join(current_lines).strip()
                current_key = next_key
                current_lines = []
                continue
        if current_key is not None:
            current_lines.append(line)
    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()
    return sections


def _read_required(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"PUSH_PREVIEW_FILE_MISSING:{path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"PUSH_PREVIEW_FILE_EMPTY:{path}")
    return text


def _escape_table(value: str) -> str:
    return " ".join(value.replace("|", "/").split())


def _human_markdown_blocks(message: PushMessage) -> tuple[str, ...]:
    sections = _parse_inner_sections(message.text)
    if message.key == "intraday":
        return _intraday_blocks(message.text, sections)
    if message.key == "morning_build":
        return _simple_blocks("今日建仓", message.text)
    if message.key == "positions":
        return _position_blocks(message.text, sections)
    if message.key == "watchlist":
        return _simple_blocks("建仓结论", message.text)
    if message.key == "news":
        return _simple_blocks("消息结论", message.text)
    if message.key == "overnight_arbitrage":
        return _simple_blocks("隔夜套利", message.text)
    if message.key == "evening_review":
        return _simple_blocks("复盘结论", message.text)
    if message.key == "next_day":
        return _next_day_blocks(message.text, sections)
    if message.key == "risk":
        return _simple_blocks("风险提示", message.text)
    raise ValueError(f"PUSH_MESSAGE_KEY_UNSUPPORTED:{message.key}")


def _intraday_blocks(text: str, sections: dict[str, list[str]]) -> tuple[str, ...]:
    all_lines = _human_lines(_visible_section_lines(sections) or text.splitlines())
    conclusion = _lines_by_prefix(all_lines, ("- 结论：", "- 策略："))
    market = _human_lines(sections.get("市场状态", []))
    mainline = _human_lines(sections.get("主线判断", []))
    cause = _human_lines(sections.get("消息驱动分析", []))
    scenario = _human_lines(sections.get("早盘走势推演", []))
    message_impact = _lines_by_prefix(scenario, ("- 消息影响：",))
    hot = _hot_board_bullets(_table_rows(sections.get("当天热门板块", [])))
    funding = _lines_by_prefix(market, ("- 资金：",)) + hot
    return _compact_blocks(
        _block("先看结论", conclusion or _focus_lines(message_key="intraday", lines=all_lines)),
        _block("原因分析", cause),
        _block("消息影响", message_impact),
        _block("市场状态", market),
        _block("主线判断", mainline),
        _block("资金与热点", funding),
        _block("今日走势推演", scenario),
    )


def _next_day_blocks(text: str, sections: dict[str, list[str]]) -> tuple[str, ...]:
    lines = _human_lines(_visible_section_lines(sections) or text.splitlines())
    plan = _human_lines(sections.get("次日操作计划", []))
    scenario = _human_lines(sections.get("早盘走势推演", []))
    message_impact = _lines_by_prefix(scenario, ("- 消息影响：",))
    return _compact_blocks(
        _block("先看结论", _focus_lines(message_key="next_day", lines=lines)),
        _block("明日计划", plan),
        _block("消息影响", message_impact),
        _block("今日走势推演", scenario),
    )


def _position_blocks(text: str, sections: dict[str, list[str]]) -> tuple[str, ...]:
    conclusion = _human_lines(sections.get("仓位调整建议", []))
    actions = _position_table_bullets(_table_rows(text.splitlines()))
    if not conclusion:
        conclusion = _focus_lines(message_key="positions", lines=_human_lines(text.splitlines()))
    return _compact_blocks(
        _block("仓位结论", conclusion),
        _block("个股动作", actions),
    )


def _simple_blocks(title: str, text: str) -> tuple[str, ...]:
    sections = _parse_inner_sections(text)
    lines = _human_lines(_visible_section_lines(sections) or text.splitlines())
    return _compact_blocks(_block("先看结论", _focus_lines(message_key="", lines=lines)), _block(title, lines))


def _focus_text(message: PushMessage) -> str:
    lines = _clean_content_lines(message.text)
    selected: list[str] = []
    for prefix in FOCUS_PREFIXES[message.key]:
        for line in lines:
            if line.startswith(prefix) and line not in selected:
                selected.append(line)
                break
    if not selected:
        selected = lines[:3]
    return "\n".join(selected) if selected else "详见完整内容。"


def _clean_content_lines(text: str) -> list[str]:
    result: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("|"):
            continue
        result.append(line)
    return result


def _parse_inner_sections(text: str) -> dict[str, list[str]]:
    current_title: str | None = None
    current_lines: list[str] = []
    sections: dict[str, list[str]] = {}
    for line in text.splitlines():
        if line.startswith("## "):
            if current_title is not None:
                sections[current_title] = current_lines
            current_title = line.removeprefix("## ").strip()
            current_lines = []
            continue
        if current_title is not None:
            current_lines.append(line)
    if current_title is not None:
        sections[current_title] = current_lines
    return sections


def _visible_section_lines(sections: dict[str, list[str]]) -> list[str]:
    lines: list[str] = []
    for title, section_lines in sections.items():
        if title in HIDDEN_SECTION_TITLES:
            continue
        lines.extend(section_lines)
    return lines


def _human_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or _is_table_line(line):
            continue
        result.append(line if line.startswith("- ") else f"- {line}")
    return result


def _focus_lines(message_key: str, lines: list[str]) -> list[str]:
    prefixes = FOCUS_PREFIXES.get(message_key, ())
    selected = _lines_by_prefix(lines, prefixes) if prefixes else []
    return selected or lines[:3]


def _lines_by_prefix(lines: list[str], prefixes: tuple[str, ...]) -> list[str]:
    selected: list[str] = []
    for prefix in prefixes:
        for line in lines:
            if line.startswith(prefix) and line not in selected:
                selected.append(line)
                break
    return selected


def _block(title: str, lines: list[str]) -> str | None:
    clean_lines = [line for line in lines if line.strip()]
    if not clean_lines:
        return None
    return f"**{title}**\n" + "\n".join(clean_lines)


def _compact_blocks(*blocks: str | None) -> tuple[str, ...]:
    result = tuple(block for block in blocks if block is not None)
    if not result:
        raise ValueError("PUSH_CARD_CONTENT_EMPTY")
    return result


def _table_rows(lines: list[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    headers: list[str] | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if not _is_table_line(line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        if headers is None:
            headers = cells
            continue
        rows.append(dict(zip(headers, cells)))
    return rows


def _hot_board_bullets(rows: list[dict[str, str]]) -> list[str]:
    result: list[str] = []
    for row in rows[:3]:
        board = row.get("板块", "")
        if not board:
            continue
        result.append(
            f"- {board}：今日 {row.get('今日', '未记录')}，20日 {row.get('20日涨幅', '未记录')}，标签 {row.get('标签', '未记录')}"
        )
    return result


def _position_table_bullets(rows: list[dict[str, str]]) -> list[str]:
    result: list[str] = []
    for row in rows:
        stock = row.get("股票", "")
        action = row.get("建议", "")
        if not stock or not action:
            continue
        stock_name = stock.split("(", 1)[0]
        result.append(
            f"- {stock_name}：{action}，浮盈亏 {row.get('浮盈亏', '未记录')}，"
            f"现价 {row.get('现价', '未记录')}，止损 {row.get('止损', '未记录')}"
        )
    return result


def _is_table_line(line: str) -> bool:
    return line.startswith("|") and line.endswith("|")


def _payload_size(payload: FeishuPayload) -> int:
    return len(json.dumps(payload, ensure_ascii=False))
