"""A1-02 意图识别的数据合同：8 类意图、槽位白名单、结构化结果与评测用例。

本模块只做结构与取值校验，不调用模型、不访问网络、不注册任何工具。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .contracts import (
    ContractError,
    INTENT_SOURCES,
    require_choice,
    require_int,
    require_json_value,
    require_mapping,
    require_number,
    require_sequence,
    require_str,
    validate_against_schema,
)
from .data.models import canonical_json, sha256_text

INTENT_SCHEMA_VERSION = 1

#: 固定 8 类意图，顺序即文档顺序，禁止在运行时增删。
INTENT_CLASSES: tuple[str, ...] = (
    "pos_question",
    "report_generate",
    "push_request",
    "history_question",
    "market_check",
    "risk_question",
    "code_task",
    "fallback",
)

INTENT_LABELS: Mapping[str, str] = {
    "pos_question": "问持仓：成本、盈亏、仓位、某只票在自己账户里的表现",
    "report_generate": "要求生成或查看当日分析报告（日报、早盘、尾盘、复盘）",
    "push_request": "要求推送相关动作：预览推送内容、发送到飞书、查看推送时间表或推送状态",
    "history_question": "问历史：以前某天的报告、审计记录、试运行结果、期望交易日",
    "market_check": "问行情：大盘、板块、指数、个股盘面、新闻快讯本身",
    "risk_question": "问风控：止损线、单票与总仓位上限、风险模式、是否触发风控",
    "code_task": "要求执行代码或计算：算相关性、跑回测、自定义选股、脚本执行",
    "fallback": "无法归类或证据不足，必须交回用户澄清，禁止猜测",
}

DEFAULT_MIN_CONFIDENCE = 0.5

#: 槽位白名单，其余槽位一律拒绝。
SLOT_NAMES: tuple[str, ...] = ("as_of", "stock_code", "slot", "top_k")

SPLITS: tuple[str, ...] = ("train", "dev", "test")

MAX_QUERY_LEN = 500
MIN_TOP_K = 1
MAX_TOP_K = 20

STOCK_CODE_PATTERN = re.compile(r"^(?:(?:SH|SZ|BJ)\d{6}|[0368]\d{5})$")
DATE_TEXT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

INTENT_ERROR_CODES: tuple[str, ...] = (
    "INTENT_INVALID",
    "INTENT_TEXT_EMPTY",
    "INTENT_TEXT_TOO_LONG",
    "INTENT_LABEL_UNKNOWN",
    "INTENT_SLOT_UNKNOWN",
    "INTENT_SLOT_INVALID",
    "INTENT_CONFIDENCE_INVALID",
    "INTENT_L2_UNAVAILABLE",
    "INTENT_CONFIDENCE_LOW",
    "INTENT_CASE_INVALID",
)


class IntentError(ContractError):
    """意图合同错误，code 取值见 INTENT_ERROR_CODES。"""


def push_slot_keys() -> tuple[str, ...]:
    """推送时段白名单直接复用核心包，避免两处清单分叉。"""
    from personal_quant.notifier.schedule import PUSH_SLOTS

    return tuple(slot.key for slot in PUSH_SLOTS)


# ---------------------------------------------------------------------------
# 槽位校验
# ---------------------------------------------------------------------------


def validate_query(text: Any, *, where: str = "query") -> str:
    if not isinstance(text, str):
        raise IntentError("INTENT_INVALID", f"{where} 需要字符串，实际 {type(text).__name__}")
    stripped = text.strip()
    if not stripped:
        raise IntentError("INTENT_TEXT_EMPTY", f"{where} 不能为空")
    if len(stripped) > MAX_QUERY_LEN:
        raise IntentError(
            "INTENT_TEXT_TOO_LONG", f"{where} 长度 {len(stripped)} 超过上限 {MAX_QUERY_LEN}"
        )
    return stripped


def validate_as_of(value: Any, *, where: str = "as_of") -> str:
    if isinstance(value, dt.datetime):
        raise IntentError("INTENT_SLOT_INVALID", f"{where} 不接受 datetime，只接受日期")
    if isinstance(value, dt.date):
        return value.isoformat()
    text = require_str(value, where)
    if not DATE_TEXT_PATTERN.match(text):
        raise IntentError("INTENT_SLOT_INVALID", f"{where} 需要 YYYY-MM-DD，实际 {text!r}")
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise IntentError("INTENT_SLOT_INVALID", f"{where} 不是合法日期：{text!r}") from exc
    return parsed.isoformat()


def validate_stock_code(value: Any, *, where: str = "stock_code") -> str:
    text = require_str(value, where).strip().upper()
    if not STOCK_CODE_PATTERN.match(text):
        raise IntentError(
            "INTENT_SLOT_INVALID", f"{where} 需要 6 位股票代码或 SH/SZ/BJ 前缀，实际 {text!r}"
        )
    return text


def validate_slot(value: Any, *, where: str = "slot") -> str:
    text = require_str(value, where).strip()
    allowed = push_slot_keys()
    if text not in allowed:
        raise IntentError("INTENT_SLOT_INVALID", f"{where} 必须是 {list(allowed)} 之一，实际 {text!r}")
    return text


def validate_top_k(value: Any, *, where: str = "top_k") -> int:
    if isinstance(value, bool):
        raise IntentError("INTENT_SLOT_INVALID", f"{where} 不接受布尔值")
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise IntentError("INTENT_SLOT_INVALID", f"{where} 需要整数，实际 {value!r}")
        value = int(text)
    if isinstance(value, str):
        pass
    if not isinstance(value, int) or isinstance(value, bool):
        raise IntentError("INTENT_SLOT_INVALID", f"{where} 需要整数，实际 {value!r}")
    if value < MIN_TOP_K:
        # 合同要求：意图层所有槽位错误都必须是 IntentError，不能漏出底层 ContractError
        raise IntentError("INTENT_SLOT_INVALID", f"{where} 最小 {MIN_TOP_K}，实际 {value}")
    number = value
    if number > MAX_TOP_K:
        raise IntentError("INTENT_SLOT_INVALID", f"{where} 最大 {MAX_TOP_K}，实际 {number}")
    return number


_SLOT_VALIDATORS = {
    "as_of": validate_as_of,
    "stock_code": validate_stock_code,
    "slot": validate_slot,
    "top_k": validate_top_k,
}


def validate_slots(slots: Any, *, where: str = "slots") -> dict[str, Any]:
    data = require_mapping(slots, where)
    unknown = sorted(str(key) for key in data if key not in SLOT_NAMES)
    if unknown:
        raise IntentError("INTENT_SLOT_UNKNOWN", f"{where} 出现白名单外槽位 {unknown}")
    normalized: dict[str, Any] = {}
    for key, value in data.items():
        if value is None:
            continue
        normalized[key] = _SLOT_VALIDATORS[key](value, where=f"{where}.{key}")
    return normalized


# ---------------------------------------------------------------------------
# 识别结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentResult:
    """一次意图识别的结果；只描述"听懂什么"，不携带任何工具调用。"""

    intent: str
    confidence: float
    source: str
    slots: Mapping[str, Any] = field(default_factory=dict)
    rationale: str = ""
    errors: tuple[str, ...] = ()
    low_confidence: bool = False
    latency_ms: float = 0.0
    model: str = ""
    schema_version: int = INTENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != INTENT_SCHEMA_VERSION:
            raise IntentError(
                "INTENT_INVALID",
                f"schema_version 期望 {INTENT_SCHEMA_VERSION}，实际 {self.schema_version}",
            )
        require_choice(self.intent, "intent", INTENT_CLASSES)
        require_choice(self.source, "source", INTENT_SOURCES)
        confidence = require_number(self.confidence, "confidence", minimum=0.0)
        if confidence > 1.0:
            raise IntentError("INTENT_CONFIDENCE_INVALID", f"confidence 最大 1.0，实际 {confidence}")
        require_str(self.rationale, "rationale", allow_empty=True, max_len=500)
        require_number(self.latency_ms, "latency_ms", minimum=0.0)
        require_str(self.model, "model", allow_empty=True, max_len=120)
        object.__setattr__(self, "slots", validate_slots(dict(self.slots)))
        object.__setattr__(self, "errors", tuple(str(item) for item in self.errors))

    @property
    def accepted(self) -> bool:
        """是否可以直接进入路由；fallback、低置信度和任何错误码都视为不可路由。"""
        return self.intent != "fallback" and not self.low_confidence and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "intent": self.intent,
            "confidence": self.confidence,
            "source": self.source,
            "slots": dict(self.slots),
            "rationale": self.rationale,
            "errors": list(self.errors),
            "low_confidence": self.low_confidence,
            "latency_ms": self.latency_ms,
            "model": self.model,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IntentResult":
        payload = require_mapping(data, "IntentResult")
        extra = sorted(set(payload) - set(INTENT_RESULT_FIELDS))
        if extra:
            raise IntentError("INTENT_INVALID", f"IntentResult 出现未知字段 {extra}")
        missing = sorted(set(INTENT_RESULT_FIELDS) - set(payload))
        if missing:
            raise IntentError("INTENT_INVALID", f"IntentResult 缺少字段 {missing}")
        return cls(
            intent=payload["intent"],
            confidence=payload["confidence"],
            source=payload["source"],
            slots=payload["slots"],
            rationale=payload["rationale"],
            errors=tuple(payload["errors"]),
            low_confidence=payload["low_confidence"],
            latency_ms=payload["latency_ms"],
            model=payload["model"],
            schema_version=payload["schema_version"],
        )


INTENT_RESULT_FIELDS: tuple[str, ...] = (
    "schema_version",
    "intent",
    "confidence",
    "source",
    "slots",
    "rationale",
    "errors",
    "low_confidence",
    "latency_ms",
    "model",
)


# ---------------------------------------------------------------------------
# 评测用例
# ---------------------------------------------------------------------------


def case_content_hash(text: str, label: str, slots: Mapping[str, Any]) -> str:
    """用例指纹只覆盖"文本 + 标签 + 槽位"，改数据必然改 hash。"""
    return sha256_text(canonical_json({"text": text, "label": label, "slots": dict(slots)}))


@dataclass(frozen=True)
class IntentCase:
    """一条标注用例：带来源、数据集版本、切分和内容 hash。"""

    case_id: str
    text: str
    label: str
    slots: Mapping[str, Any] = field(default_factory=dict)
    source: str = ""
    dataset_version: str = ""
    split: str = ""
    content_hash: str = ""
    annotation: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = INTENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != INTENT_SCHEMA_VERSION:
            raise IntentError(
                "INTENT_CASE_INVALID",
                f"schema_version 期望 {INTENT_SCHEMA_VERSION}，实际 {self.schema_version}",
            )
        require_str(self.case_id, "case_id", max_len=120)
        text = validate_query(self.text, where="text")
        require_choice(self.label, "label", INTENT_CLASSES)
        require_choice(self.split, "split", SPLITS)
        require_str(self.source, "source", max_len=200)
        require_str(self.dataset_version, "dataset_version", max_len=80)
        normalized_slots = validate_slots(dict(self.slots))
        annotation = _validate_annotation(self.annotation)
        expected_hash = case_content_hash(text, self.label, normalized_slots)
        if self.content_hash and self.content_hash != expected_hash:
            raise IntentError(
                "INTENT_CASE_INVALID",
                f"case_id={self.case_id} content_hash 与内容不一致，期望 {expected_hash[:12]}",
            )
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "slots", normalized_slots)
        object.__setattr__(self, "annotation", annotation)
        object.__setattr__(self, "content_hash", expected_hash)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "text": self.text,
            "label": self.label,
            "slots": dict(self.slots),
            "source": self.source,
            "dataset_version": self.dataset_version,
            "split": self.split,
            "content_hash": self.content_hash,
            "annotation": dict(self.annotation),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "IntentCase":
        payload = require_mapping(data, "IntentCase")
        extra = sorted(set(payload) - set(INTENT_CASE_FIELDS))
        if extra:
            raise IntentError("INTENT_CASE_INVALID", f"IntentCase 出现未知字段 {extra}")
        missing = sorted(set(INTENT_CASE_FIELDS) - set(payload))
        if missing:
            raise IntentError("INTENT_CASE_INVALID", f"IntentCase 缺少字段 {missing}")
        return cls(
            case_id=payload["case_id"],
            text=payload["text"],
            label=payload["label"],
            slots=payload["slots"],
            source=payload["source"],
            dataset_version=payload["dataset_version"],
            split=payload["split"],
            content_hash=payload["content_hash"],
            annotation=payload["annotation"],
            schema_version=payload["schema_version"],
        )


INTENT_CASE_FIELDS: tuple[str, ...] = (
    "schema_version",
    "case_id",
    "text",
    "label",
    "slots",
    "source",
    "dataset_version",
    "split",
    "content_hash",
    "annotation",
)

ANNOTATION_FIELDS: tuple[str, ...] = ("rounds", "disagreement", "note")


def _validate_annotation(value: Any) -> dict[str, Any]:
    data = require_mapping(value, "annotation")
    extra = sorted(set(data) - set(ANNOTATION_FIELDS))
    if extra:
        raise IntentError("INTENT_CASE_INVALID", f"annotation 出现未知字段 {extra}")
    missing = sorted(set(ANNOTATION_FIELDS) - set(data))
    if missing:
        raise IntentError("INTENT_CASE_INVALID", f"annotation 缺少字段 {missing}")
    rounds = []
    for index, raw in enumerate(require_sequence(data["rounds"], "annotation.rounds")):
        item = require_mapping(raw, f"annotation.rounds[{index}]")
        item_extra = sorted(set(item) - {"annotator", "label", "slots", "note"})
        if item_extra:
            raise IntentError(
                "INTENT_CASE_INVALID", f"annotation.rounds[{index}] 出现未知字段 {item_extra}"
            )
        for key in ("annotator", "label", "note"):
            if key not in item:
                raise IntentError(
                    "INTENT_CASE_INVALID", f"annotation.rounds[{index}] 缺少字段 {key}"
                )
        rounds.append(
            {
                "annotator": require_str(item["annotator"], f"rounds[{index}].annotator", max_len=80),
                "label": require_choice(item["label"], f"rounds[{index}].label", INTENT_CLASSES),
                "note": require_str(
                    item["note"], f"rounds[{index}].note", allow_empty=True, max_len=200
                ),
            }
        )
    if not rounds:
        raise IntentError("INTENT_CASE_INVALID", "annotation.rounds 不能为空")
    disagreement = bool(data["disagreement"])
    actual = any(item["label"] != rounds[0]["label"] for item in rounds)
    if disagreement != actual:
        raise IntentError(
            "INTENT_CASE_INVALID",
            f"annotation.disagreement={disagreement} 与轮次标注不一致（实际 {actual}）",
        )
    return {
        "rounds": rounds,
        "disagreement": disagreement,
        "note": require_str(data["note"], "annotation.note", allow_empty=True, max_len=200),
    }


# ---------------------------------------------------------------------------
# JSON Schema
# ---------------------------------------------------------------------------


def _object_schema(title: str, properties: Mapping[str, Any], required: Sequence[str]) -> dict[str, Any]:
    return {
        "title": title,
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": dict(properties),
    }


INTENT_RESULT_JSON_SCHEMA = _object_schema(
    "IntentResult",
    {
        "schema_version": {"type": "integer", "minimum": 1},
        "intent": {"type": "string", "enum": list(INTENT_CLASSES)},
        "confidence": {"type": "number", "minimum": 0.0},
        "source": {"type": "string", "enum": list(INTENT_SOURCES)},
        "slots": {"type": "object"},
        "rationale": {"type": "string"},
        "errors": {"type": "array"},
        "low_confidence": {"type": "boolean"},
        "latency_ms": {"type": "number", "minimum": 0.0},
        "model": {"type": "string"},
    },
    INTENT_RESULT_FIELDS,
)

INTENT_CASE_JSON_SCHEMA = _object_schema(
    "IntentCase",
    {
        "schema_version": {"type": "integer", "minimum": 1},
        "case_id": {"type": "string"},
        "text": {"type": "string"},
        "label": {"type": "string", "enum": list(INTENT_CLASSES)},
        "slots": {"type": "object"},
        "source": {"type": "string"},
        "dataset_version": {"type": "string"},
        "split": {"type": "string", "enum": list(SPLITS)},
        "content_hash": {"type": "string"},
        "annotation": {"type": "object"},
    },
    INTENT_CASE_FIELDS,
)

JSON_SCHEMAS: dict[str, dict[str, Any]] = {
    "IntentResult": INTENT_RESULT_JSON_SCHEMA,
    "IntentCase": INTENT_CASE_JSON_SCHEMA,
}
