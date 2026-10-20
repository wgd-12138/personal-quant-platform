from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError


FEISHU_CARD_TEMPLATES = {"blue", "wathet", "turquoise", "green", "yellow", "orange", "red", "purple", "grey"}


@dataclass(frozen=True)
class FeishuWebhookResult:
    status_code: int
    response_text: str


FeishuPayload = dict[str, Any]


def build_feishu_text_payload(text: str) -> dict[str, Any]:
    if not text.strip():
        raise ValueError("FEISHU_MESSAGE_EMPTY")
    return {"msg_type": "text", "content": {"text": text}}


def build_feishu_card_payload(
    title: str,
    markdown_blocks: tuple[str, ...],
    template: str = "blue",
) -> FeishuPayload:
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("FEISHU_CARD_TITLE_EMPTY")
    if template not in FEISHU_CARD_TEMPLATES:
        raise ValueError(f"FEISHU_CARD_TEMPLATE_INVALID:{template}")
    blocks = tuple(block.strip() for block in markdown_blocks if block.strip())
    if not blocks:
        raise ValueError("FEISHU_CARD_CONTENT_EMPTY")
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template,
                "title": {"tag": "plain_text", "content": clean_title},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": block}}
                for block in blocks
            ],
        },
    }


def send_feishu_text(webhook_url: str, text: str, timeout: float = 10.0) -> FeishuWebhookResult:
    return send_feishu_payload(webhook_url, build_feishu_text_payload(text), timeout=timeout)


def send_feishu_payload(webhook_url: str, payload: FeishuPayload, timeout: float = 10.0) -> FeishuWebhookResult:
    if not webhook_url.strip():
        raise ValueError("FEISHU_WEBHOOK_URL_EMPTY")
    if not payload:
        raise ValueError("FEISHU_PAYLOAD_EMPTY")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            response_text = response.read().decode("utf-8")
            status_code = response.status
    except HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"FEISHU_WEBHOOK_HTTP_ERROR:{exc.code}:{response_text}") from exc
    except URLError as exc:
        raise RuntimeError(f"FEISHU_WEBHOOK_NETWORK_ERROR:{exc.reason}") from exc
    _assert_success_response(status_code, response_text)
    return FeishuWebhookResult(status_code=status_code, response_text=response_text)


def _assert_success_response(status_code: int, response_text: str) -> None:
    if status_code < 200 or status_code >= 300:
        raise RuntimeError(f"FEISHU_WEBHOOK_HTTP_STATUS:{status_code}:{response_text}")
    if not response_text.strip():
        raise RuntimeError("FEISHU_WEBHOOK_EMPTY_RESPONSE")
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"FEISHU_WEBHOOK_INVALID_JSON:{response_text}") from exc
    code = data.get("code", data.get("StatusCode", 0))
    if code != 0:
        message = data.get("msg", data.get("StatusMessage", response_text))
        raise RuntimeError(f"FEISHU_WEBHOOK_FAILED:{code}:{message}")
