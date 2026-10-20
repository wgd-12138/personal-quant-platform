from __future__ import annotations

import datetime as dt


PUSH_MESSAGES = [
    ("morning_build", "早盘建仓推荐", "集合竞价后"),
    ("intraday", "盘中实时盘面", "盘中或手动刷新"),
    ("positions", "持仓个股操作", "盘中或手动刷新"),
    ("watchlist", "建仓候选推荐", "盘中或手动刷新"),
    ("news", "利好利空捕捉", "盘中或手动刷新"),
    ("overnight_arbitrage", "尾盘隔夜套利", "尾盘"),
    ("evening_review", "晚间复盘", "收盘后"),
    ("next_day", "次日操作计划", "收盘后"),
    ("risk", "风险提示", "收盘后"),
]


def build_push_preview_report(
    as_of: dt.date,
    sections: dict[str, str],
    strict_ready: bool,
    readiness_errors: list[str],
    focus_positions: bool = True,
    brief_sections: dict[str, str] | None = None,
) -> str:
    gap_text = "、".join(readiness_errors) if readiness_errors else "暂无"
    status_text = "完整" if strict_ready else "不完整"
    rows = [
        f"# 每日推送预览（{as_of.isoformat()}）",
        "",
        f"- 数据：{status_text}；缺口：{gap_text}",
        "",
    ]

    message_sections = brief_sections or sections
    for key, title, _trigger in PUSH_MESSAGES:
        content = _strip_report_title(_message_content(key, message_sections))
        content = _strip_message_title(content, title)
        rows.extend([f"## {title}", content.strip(), ""])
    return "\n".join(rows).rstrip() + "\n"


def _message_content(key: str, sections: dict[str, str]) -> str:
    if key == "evening_review":
        return _slice_between(sections["evening"], "## 晚间复盘", "## 次日操作计划")
    if key == "next_day":
        return _slice_between(sections["evening"], "## 次日操作计划", "## 风险提示")
    if key == "risk":
        return _slice_from(sections["evening"], "## 风险提示")
    return sections[key]


def _slice_from(text: str, start_heading: str) -> str:
    start = text.find(start_heading)
    if start < 0:
        raise ValueError(f"推送预览缺少章节: {start_heading}")
    return text[start:]


def _slice_between(text: str, start_heading: str, end_heading: str) -> str:
    start = text.find(start_heading)
    if start < 0:
        raise ValueError(f"推送预览缺少章节: {start_heading}")
    end = text.find(end_heading, start + len(start_heading))
    if end < 0:
        return text[start:]
    return text[start:end]


def _strip_report_title(text: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    return "\n".join(lines)


def _strip_message_title(text: str, title: str) -> str:
    lines = text.strip().splitlines()
    if lines and lines[0].strip() == f"## {title}":
        lines = lines[1:]
    while lines and not lines[0].strip():
        lines = lines[1:]
    return "\n".join(lines)
