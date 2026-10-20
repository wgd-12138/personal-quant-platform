from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from personal_quant.notifier.feishu import FeishuPayload, FeishuWebhookResult, build_feishu_card_payload, send_feishu_payload
from personal_quant.notifier.webhook_env import resolve_feishu_webhook


Sender = Callable[[str, FeishuPayload], FeishuWebhookResult]


@dataclass(frozen=True)
class PushChannelTestResult:
    sent_at: dt.datetime
    dry_run: bool
    message: FeishuPayload
    response: FeishuWebhookResult | None
    webhook_fingerprint: str | None


def send_push_channel_test(
    output_dir: Path,
    webhook_url: str | None = None,
    live: bool = False,
    sender: Sender = send_feishu_payload,
    now: dt.datetime | None = None,
) -> Path:
    sent_at = (now if now is not None else dt.datetime.now()).replace(microsecond=0)
    message = _build_test_message(sent_at)
    response: FeishuWebhookResult | None = None
    webhook_fingerprint: str | None = None
    if live:
        resolved = resolve_feishu_webhook(webhook_url)
        if resolved.url is None:
            raise ValueError("FEISHU_WEBHOOK_URL_REQUIRED")
        webhook_fingerprint = webhook_url_fingerprint(resolved.url)
        response = sender(resolved.url, message)
    result = PushChannelTestResult(
        sent_at=sent_at,
        dry_run=not live,
        message=message,
        response=response,
        webhook_fingerprint=webhook_fingerprint,
    )
    return write_push_channel_test_report(result, output_dir)


def write_push_channel_test_report(result: PushChannelTestResult, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "push_channel_test.md"
    output_path.write_text(build_push_channel_test_report(result), encoding="utf-8")
    return output_path


def build_push_channel_test_report(result: PushChannelTestResult) -> str:
    live_passed = not result.dry_run and result.response is not None
    rows = [
        f"# 推送通道测试（{result.sent_at.strftime('%Y-%m-%d %H:%M:%S')}）",
        "",
        "## 总判断",
        f"- 模式：{'dry-run' if result.dry_run else 'live'}",
        "- 通道：飞书 webhook",
        f"- 发送状态：{'未发送，仅检查内容' if result.dry_run else '已发送'}",
        f"- 结论：{'通过' if live_passed else '未验证'}",
        f"- Webhook 指纹：{result.webhook_fingerprint if result.webhook_fingerprint else '未记录'}",
        "",
        "## 测试消息",
        "- 格式：飞书卡片",
        f"- 字数：{len(json.dumps(result.message, ensure_ascii=False))}",
        "- 内容：个人量化推送通道测试，不包含交易结论。",
    ]
    if result.response is not None:
        rows.extend(
            [
                "",
                "## 飞书响应",
                "| HTTP | 响应 |",
                "|---:|---|",
                f"| {result.response.status_code} | {_escape_table(result.response.response_text)} |",
            ]
        )
    return "\n".join(rows) + "\n"


def _build_test_message(sent_at: dt.datetime) -> FeishuPayload:
    return build_feishu_card_payload(
        title=f"个人量化｜推送通道测试｜{sent_at.strftime('%Y-%m-%d %H:%M:%S')}",
        template="blue",
        markdown_blocks=(
            "**结果**：如果你看到这张卡片，飞书 webhook 已能接收当前推送格式。",
            "**边界**：这只是通道测试，不包含交易结论。",
        ),
    )


def webhook_url_fingerprint(webhook_url: str) -> str:
    cleaned = webhook_url.strip()
    if not cleaned:
        raise ValueError("FEISHU_WEBHOOK_URL_EMPTY")
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]


def _escape_table(value: str) -> str:
    return " ".join(value.replace("|", "/").split())
