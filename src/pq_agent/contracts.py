"""pq_agent 可序列化结构合同（contracts）。

本模块是 agent 运行时全部跨边界结构的唯一来源：
ToolCall / ToolResult / Citation / Approval / Receipt / Error / DataQuality / Budget。

约束（AGENT_ENHANCEMENT_PLAN 6.1 与 A1-01 任务合同）：

1. 每个结构都带 schema_version，恢复时逐项校验，不做静默兼容。
2. 所有结构必须能 JSON round-trip；非法类型显式拒绝，不做隐式转换。
3. 缺字段、类型错误、枚举越界都抛带错误码的 ContractError，便于评测和 trace 断言。
4. 只依赖标准库与 pq_agent.security，不引入第三方依赖。
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

CONTRACTS_SCHEMA_VERSION = 1
TOOL_CALL_SCHEMA_VERSION = 1
TOOL_RESULT_SCHEMA_VERSION = 1
CITATION_SCHEMA_VERSION = 1
APPROVAL_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
ERROR_SCHEMA_VERSION = 1
DATA_QUALITY_SCHEMA_VERSION = 1
BUDGET_SCHEMA_VERSION = 1
MESSAGE_SCHEMA_VERSION = 1

CAPABILITIES = (
    "read_local",
    "read_external",
    "control_write",
    "business_write",
    "notify_external",
    # code_exec 是第六类能力：它既不是只读，也不是写盘或外发，而是「在沙箱里执行代码」。
    # 它必须登记在这里，否则 ToolCall / Approval / ApprovalRecord 的 capability 校验
    # 会把合法的代码执行调用判成非法参数（A6-03 三.1：四处枚举必须同名同义）。
    # 注意它与 SIDE_EFFECT_CAPABILITIES 分开：代码执行走审批，但它的副作用是「执行」，
    # 不是「写了哪个业务文件」，混在一起会让风险说明和回滚说明都失真。
    "code_exec",
)
APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired")
RECEIPT_STATUSES = ("committed", "failed", "rejected", "skipped")
INTENT_SOURCES = ("l1", "l2", "fallback")
MESSAGE_ROLES = ("user", "assistant", "system", "tool", "summary")

CONTRACT_SCHEMA_VERSIONS = {
    "message": MESSAGE_SCHEMA_VERSION,
    "tool_call": TOOL_CALL_SCHEMA_VERSION,
    "tool_result": TOOL_RESULT_SCHEMA_VERSION,
    "citation": CITATION_SCHEMA_VERSION,
    "approval": APPROVAL_SCHEMA_VERSION,
    "receipt": RECEIPT_SCHEMA_VERSION,
    "error": ERROR_SCHEMA_VERSION,
    "data_quality": DATA_QUALITY_SCHEMA_VERSION,
    "budget": BUDGET_SCHEMA_VERSION,
}

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_HEX12 = re.compile(r"\A[0-9a-f]{6,64}\Z")


class ContractError(ValueError):
    """带错误码的合同校验异常。错误码写在消息前缀，便于断言与 trace。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        text = f"{code}: {detail}" if detail else code
        super().__init__(text)


# --------------------------------------------------------------------------
# 基础校验helper
# --------------------------------------------------------------------------


def require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("TYPE_INVALID", f"{where} 需要对象，实际 {type(value).__name__}")
    return value


def require_sequence(value: Any, where: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ContractError("TYPE_INVALID", f"{where} 需要数组，实际 {type(value).__name__}")
    return value


def require_str(value: Any, where: str, *, allow_empty: bool = False, max_len: int | None = None) -> str:
    if not isinstance(value, str):
        raise ContractError("TYPE_INVALID", f"{where} 需要字符串，实际 {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise ContractError("EMPTY_FIELD", f"{where} 不能为空")
    if max_len is not None and len(value) > max_len:
        raise ContractError("FIELD_TOO_LONG", f"{where} 长度 {len(value)} 超过上限 {max_len}")
    return value


def optional_str(value: Any, where: str, *, max_len: int | None = None) -> str | None:
    if value is None:
        return None
    return require_str(value, where, allow_empty=True, max_len=max_len)


def require_int(value: Any, where: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError("TYPE_INVALID", f"{where} 需要整数，实际 {type(value).__name__}")
    if minimum is not None and value < minimum:
        raise ContractError("FIELD_OUT_OF_RANGE", f"{where}={value} 小于下限 {minimum}")
    return value


def require_number(value: Any, where: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("TYPE_INVALID", f"{where} 需要数字，实际 {type(value).__name__}")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ContractError("FIELD_OUT_OF_RANGE", f"{where}={number} 小于下限 {minimum}")
    return number


def require_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError("TYPE_INVALID", f"{where} 需要布尔值，实际 {type(value).__name__}")
    return value


def require_choice(value: Any, where: str, choices: Sequence[str]) -> str:
    text = require_str(value, where)
    if text not in choices:
        raise ContractError("ENUM_INVALID", f"{where}='{text}'；只允许 {' / '.join(choices)}")
    return text


def require_iso_datetime(value: Any, where: str) -> dt.datetime:
    text = require_str(value, where)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractError("DATETIME_INVALID", f"{where}='{text}'：{exc}") from exc
    if parsed.tzinfo is None:
        raise ContractError("DATETIME_NAIVE", f"{where}='{text}' 缺少时区")
    return parsed


def iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ContractError("DATETIME_NAIVE", "写入 state 的时间必须带时区")
    return value.isoformat()


def require_hash64(value: Any, where: str) -> str:
    text = require_str(value, where)
    if not _HEX64.match(text):
        raise ContractError("HASH_INVALID", f"{where} 需要 64 位小写十六进制，实际 '{text}'")
    return text


def require_hash_prefix(value: Any, where: str) -> str:
    text = require_str(value, where)
    if not _HEX12.match(text):
        raise ContractError("HASH_INVALID", f"{where} 需要 6-64 位小写十六进制，实际 '{text}'")
    return text


def require_relative_posix_path(value: Any, where: str) -> str:
    text = require_str(value, where)
    if text.startswith(("/", "\\\\")) or ":" in text or "\\\\" in text:
        raise ContractError("PATH_NOT_RELATIVE", f"{where}='{text}' 必须是仓库内相对 POSIX 路径")
    if any(segment in ("..", ".") for segment in text.split("/")):
        raise ContractError("PATH_HAS_TRAVERSAL", f"{where}='{text}' 不允许包含 . 或 .. 段")
    return text


def require_json_value(value: Any, where: str, *, depth: int = 0) -> Any:
    """只接受可无损 JSON 序列化的值，显式拒绝 bytes / datetime / 自定义对象。"""
    if depth > 12:
        raise ContractError("JSON_DEPTH_EXCEEDED", f"{where} 嵌套超过 12 层")
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractError("JSON_KEY_INVALID", f"{where} 的键必须是字符串，实际 {type(key).__name__}")
            out[key] = require_json_value(item, f"{where}.{key}", depth=depth + 1)
        return out
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [require_json_value(item, f"{where}[{index}]", depth=depth + 1) for index, item in enumerate(value)]
    raise ContractError(
        "JSON_TYPE_UNSUPPORTED",
        f"{where} 不支持 {type(value).__name__}；只允许 JSON 原生类型",
    )


def _opt_mapping(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    return dict(require_json_value(require_mapping(value, where), where))


# --------------------------------------------------------------------------
# 最小 JSON Schema 校验器（只覆盖本文件用到的关键字）
# --------------------------------------------------------------------------


_TYPE_MAP = {
    "object": Mapping,
    "array": Sequence,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _schema_type_ok(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    if expected == "string":
        return isinstance(value, str)
    if expected == "null":
        return value is None
    raise ContractError("SCHEMA_UNSUPPORTED_KEYWORD", f"未知类型 {expected}")


def validate_against_schema(value: Any, schema: Mapping[str, Any], where: str) -> Any:
    """按最小 JSON Schema 校验；不接受任何未声明字段。"""
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_schema_type_ok(value, name) for name in types):
            raise ContractError(
                "SCHEMA_TYPE_MISMATCH",
                f"{where} 期望 {'/'.join(types)}，实际 {type(value).__name__}",
            )
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        raise ContractError("SCHEMA_ENUM_MISMATCH", f"{where}={value!r} 不在 {list(enum)} 内")
    minimum = schema.get("minimum")
    if minimum is not None and isinstance(value, (int, float)) and value < minimum:
        raise ContractError("SCHEMA_MINIMUM", f"{where}={value} 小于 {minimum}")
    if isinstance(value, str) and schema.get("minLength") is not None and len(value) < schema["minLength"]:
        raise ContractError("SCHEMA_MIN_LENGTH", f"{where} 长度不足 {schema['minLength']}")
    if isinstance(value, Mapping):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                raise ContractError("SCHEMA_REQUIRED", f"{where}.{name} 缺失")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    raise ContractError("SCHEMA_ADDITIONAL_PROPERTY", f"{where}.{name} 未在 schema 中声明")
        for name, child in properties.items():
            if name in value:
                validate_against_schema(value[name], child, f"{where}.{name}")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = schema.get("items")
        if items is not None:
            for index, item in enumerate(value):
                validate_against_schema(item, items, f"{where}[{index}]")
    return value


def _object_schema(title: str, properties: Mapping[str, Any], required: Sequence[str]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": title,
        "type": "object",
        "additionalProperties": False,
        "properties": dict(properties),
        "required": list(required),
    }


_SCHEMA_VERSION_PROPERTY = {"type": "integer", "minimum": 1}

# --------------------------------------------------------------------------
# 1. ToolCall
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """一次工具调用意图。arguments 必须先过工具 schema，禁止塞入 webhook/live/路径等禁区字段。"""

    call_id: str
    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    capability: str = "read_local"
    dry_run: bool = True
    schema_version: int = TOOL_CALL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_str(self.call_id, "ToolCall.call_id", max_len=128)
        require_str(self.tool_name, "ToolCall.tool_name", max_len=128)
        require_choice(self.capability, "ToolCall.capability", CAPABILITIES)
        require_bool(self.dry_run, "ToolCall.dry_run")
        require_int(self.schema_version, "ToolCall.schema_version", minimum=1)
        arguments = require_json_value(self.arguments, "ToolCall.arguments")
        if not isinstance(arguments, Mapping):
            raise ContractError("TYPE_INVALID", "ToolCall.arguments 需要对象")
        object.__setattr__(self, "arguments", dict(arguments))

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "capability": self.capability,
            "dry_run": self.dry_run,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ToolCall":
        source = require_mapping(data, "ToolCall")
        return cls(
            call_id=require_str(source.get("call_id"), "ToolCall.call_id", max_len=128),
            tool_name=require_str(source.get("tool_name"), "ToolCall.tool_name", max_len=128),
            arguments=_opt_mapping(source.get("arguments"), "ToolCall.arguments"),
            capability=require_choice(source.get("capability", "read_local"), "ToolCall.capability", CAPABILITIES),
            dry_run=require_bool(source.get("dry_run", True), "ToolCall.dry_run"),
            schema_version=require_int(source.get("schema_version"), "ToolCall.schema_version", minimum=1),
        )


TOOL_CALL_JSON_SCHEMA = _object_schema(
    "pq_agent.tool_call",
    {
        "call_id": {"type": "string", "minLength": 1},
        "tool_name": {"type": "string", "minLength": 1},
        "arguments": {"type": "object"},
        "capability": {"type": "string", "enum": list(CAPABILITIES)},
        "dry_run": {"type": "boolean"},
        "schema_version": _SCHEMA_VERSION_PROPERTY,
    },
    ("call_id", "tool_name", "arguments", "capability", "dry_run", "schema_version"),
)


# --------------------------------------------------------------------------
# 2. ToolResult
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolResult:
    """工具返回。summary 是给人看的脱敏摘要，data 是结构化结果。"""

    call_id: str
    tool_name: str
    ok: bool
    summary: str = ""
    error_code: str | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    artifacts: tuple[str, ...] = ()
    degraded: bool = False
    duration_ms: int | None = None
    schema_version: int = TOOL_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_str(self.call_id, "ToolResult.call_id", max_len=128)
        require_str(self.tool_name, "ToolResult.tool_name", max_len=128)
        require_bool(self.ok, "ToolResult.ok")
        optional_str(self.summary, "ToolResult.summary", max_len=2000)
        optional_str(self.error_code, "ToolResult.error_code", max_len=128)
        if self.ok and self.error_code:
            raise ContractError("RESULT_INCONSISTENT", "ok=true 时不允许带 error_code")
        if not self.ok and not self.error_code:
            raise ContractError("RESULT_INCONSISTENT", "ok=false 时必须带 error_code")
        data = require_json_value(self.data, "ToolResult.data")
        if not isinstance(data, Mapping):
            raise ContractError("TYPE_INVALID", "ToolResult.data 需要对象")
        object.__setattr__(self, "data", dict(data))
        require_bool(self.degraded, "ToolResult.degraded")
        if self.duration_ms is not None:
            require_int(self.duration_ms, "ToolResult.duration_ms", minimum=0)
        for index, item in enumerate(self.artifacts):
            require_relative_posix_path(item, f"ToolResult.artifacts[{index}]")
        object.__setattr__(self, "artifacts", tuple(self.artifacts))
        require_int(self.schema_version, "ToolResult.schema_version", minimum=1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "ok": self.ok,
            "summary": self.summary,
            "error_code": self.error_code,
            "data": dict(self.data),
            "artifacts": list(self.artifacts),
            "degraded": self.degraded,
            "duration_ms": self.duration_ms,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ToolResult":
        source = require_mapping(data, "ToolResult")
        artifacts = require_sequence(source.get("artifacts", []), "ToolResult.artifacts")
        return cls(
            call_id=require_str(source.get("call_id"), "ToolResult.call_id", max_len=128),
            tool_name=require_str(source.get("tool_name"), "ToolResult.tool_name", max_len=128),
            ok=require_bool(source.get("ok"), "ToolResult.ok"),
            summary=require_str(source.get("summary", ""), "ToolResult.summary", allow_empty=True, max_len=2000),
            error_code=optional_str(source.get("error_code"), "ToolResult.error_code", max_len=128),
            data=_opt_mapping(source.get("data"), "ToolResult.data"),
            artifacts=tuple(str(item) for item in artifacts),
            degraded=require_bool(source.get("degraded", False), "ToolResult.degraded"),
            duration_ms=source.get("duration_ms"),
            schema_version=require_int(source.get("schema_version"), "ToolResult.schema_version", minimum=1),
        )


TOOL_RESULT_JSON_SCHEMA = _object_schema(
    "pq_agent.tool_result",
    {
        "call_id": {"type": "string", "minLength": 1},
        "tool_name": {"type": "string", "minLength": 1},
        "ok": {"type": "boolean"},
        "summary": {"type": "string"},
        "error_code": {"type": ["string", "null"]},
        "data": {"type": "object"},
        "artifacts": {"type": "array", "items": {"type": "string"}},
        "degraded": {"type": "boolean"},
        "duration_ms": {"type": ["integer", "null"], "minimum": 0},
        "schema_version": _SCHEMA_VERSION_PROPERTY,
    },
    ("call_id", "tool_name", "ok", "summary", "error_code", "data", "artifacts", "degraded", "duration_ms", "schema_version"),
)


# --------------------------------------------------------------------------
# 3. Citation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """引用必须能定位到索引里的真实块：路径 + 行区间 + 内容 hash。"""

    chunk_id: str
    source_path: str
    line_start: int
    line_end: int
    content_hash: str
    title: str = ""
    schema_version: int = CITATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_str(self.chunk_id, "Citation.chunk_id", max_len=200)
        require_relative_posix_path(self.source_path, "Citation.source_path")
        require_int(self.line_start, "Citation.line_start", minimum=1)
        require_int(self.line_end, "Citation.line_end", minimum=1)
        if self.line_end < self.line_start:
            raise ContractError("FIELD_OUT_OF_RANGE", f"Citation.line_end={self.line_end} 小于 line_start={self.line_start}")
        require_hash64(self.content_hash, "Citation.content_hash")
        require_str(self.title, "Citation.title", allow_empty=True, max_len=200)
        require_int(self.schema_version, "Citation.schema_version", minimum=1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "source_path": self.source_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "content_hash": self.content_hash,
            "title": self.title,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Citation":
        source = require_mapping(data, "Citation")
        return cls(
            chunk_id=require_str(source.get("chunk_id"), "Citation.chunk_id", max_len=200),
            source_path=require_relative_posix_path(source.get("source_path"), "Citation.source_path"),
            line_start=require_int(source.get("line_start"), "Citation.line_start", minimum=1),
            line_end=require_int(source.get("line_end"), "Citation.line_end", minimum=1),
            content_hash=require_hash64(source.get("content_hash"), "Citation.content_hash"),
            title=require_str(source.get("title", ""), "Citation.title", allow_empty=True, max_len=200),
            schema_version=require_int(source.get("schema_version"), "Citation.schema_version", minimum=1),
        )


CITATION_JSON_SCHEMA = _object_schema(
    "pq_agent.citation",
    {
        "chunk_id": {"type": "string", "minLength": 1},
        "source_path": {"type": "string", "minLength": 1},
        "line_start": {"type": "integer", "minimum": 1},
        "line_end": {"type": "integer", "minimum": 1},
        "content_hash": {"type": "string", "minLength": 64},
        "title": {"type": "string"},
        "schema_version": _SCHEMA_VERSION_PROPERTY,
    },
    ("chunk_id", "source_path", "line_start", "line_end", "content_hash", "title", "schema_version"),
)


# --------------------------------------------------------------------------
# 4. Approval
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Approval:
    """人工确认记录。审批必须绑定 arguments_hash，避免批准 A 执行 B。"""

    approval_id: str
    call_id: str
    tool_name: str
    arguments_hash: str
    capability: str
    status: str = "pending"
    requested_at: str | None = None
    decided_at: str | None = None
    actor: str = ""
    reason: str = ""
    schema_version: int = APPROVAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_str(self.approval_id, "Approval.approval_id", max_len=128)
        require_str(self.call_id, "Approval.call_id", max_len=128)
        require_str(self.tool_name, "Approval.tool_name", max_len=128)
        require_hash_prefix(self.arguments_hash, "Approval.arguments_hash")
        require_choice(self.capability, "Approval.capability", CAPABILITIES)
        require_choice(self.status, "Approval.status", APPROVAL_STATUSES)
        if self.requested_at is not None:
            require_iso_datetime(self.requested_at, "Approval.requested_at")
        if self.decided_at is not None:
            require_iso_datetime(self.decided_at, "Approval.decided_at")
        if self.status == "pending" and self.decided_at is not None:
            raise ContractError("APPROVAL_INCONSISTENT", "pending 状态不允许带 decided_at")
        if self.status != "pending" and self.decided_at is None:
            raise ContractError("APPROVAL_INCONSISTENT", f"{self.status} 状态必须带 decided_at")
        require_str(self.actor, "Approval.actor", allow_empty=True, max_len=128)
        require_str(self.reason, "Approval.reason", allow_empty=True, max_len=500)
        require_int(self.schema_version, "Approval.schema_version", minimum=1)

    @property
    def decided(self) -> bool:
        return self.status != "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arguments_hash": self.arguments_hash,
            "capability": self.capability,
            "status": self.status,
            "requested_at": self.requested_at,
            "decided_at": self.decided_at,
            "actor": self.actor,
            "reason": self.reason,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Approval":
        source = require_mapping(data, "Approval")
        return cls(
            approval_id=require_str(source.get("approval_id"), "Approval.approval_id", max_len=128),
            call_id=require_str(source.get("call_id"), "Approval.call_id", max_len=128),
            tool_name=require_str(source.get("tool_name"), "Approval.tool_name", max_len=128),
            arguments_hash=require_hash_prefix(source.get("arguments_hash"), "Approval.arguments_hash"),
            capability=require_choice(source.get("capability"), "Approval.capability", CAPABILITIES),
            status=require_choice(source.get("status", "pending"), "Approval.status", APPROVAL_STATUSES),
            requested_at=optional_str(source.get("requested_at"), "Approval.requested_at"),
            decided_at=optional_str(source.get("decided_at"), "Approval.decided_at"),
            actor=require_str(source.get("actor", ""), "Approval.actor", allow_empty=True, max_len=128),
            reason=require_str(source.get("reason", ""), "Approval.reason", allow_empty=True, max_len=500),
            schema_version=require_int(source.get("schema_version"), "Approval.schema_version", minimum=1),
        )


APPROVAL_JSON_SCHEMA = _object_schema(
    "pq_agent.approval",
    {
        "approval_id": {"type": "string", "minLength": 1},
        "call_id": {"type": "string", "minLength": 1},
        "tool_name": {"type": "string", "minLength": 1},
        "arguments_hash": {"type": "string", "minLength": 6},
        "capability": {"type": "string", "enum": list(CAPABILITIES)},
        "status": {"type": "string", "enum": list(APPROVAL_STATUSES)},
        "requested_at": {"type": ["string", "null"]},
        "decided_at": {"type": ["string", "null"]},
        "actor": {"type": "string"},
        "reason": {"type": "string"},
        "schema_version": _SCHEMA_VERSION_PROPERTY,
    },
    (
        "approval_id",
        "call_id",
        "tool_name",
        "arguments_hash",
        "capability",
        "status",
        "requested_at",
        "decided_at",
        "actor",
        "reason",
        "schema_version",
    ),
)


# --------------------------------------------------------------------------
# 5. Receipt
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Receipt:
    """外发/业务写入的幂等回执。idempotency_key 决定重复执行是否跳过。"""

    receipt_id: str
    approval_id: str
    call_id: str
    tool_name: str
    status: str
    idempotency_key: str
    created_at: str | None = None
    external_id: str | None = None
    schema_version: int = RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_str(self.receipt_id, "Receipt.receipt_id", max_len=128)
        require_str(self.approval_id, "Receipt.approval_id", max_len=128)
        require_str(self.call_id, "Receipt.call_id", max_len=128)
        require_str(self.tool_name, "Receipt.tool_name", max_len=128)
        require_choice(self.status, "Receipt.status", RECEIPT_STATUSES)
        require_hash_prefix(self.idempotency_key, "Receipt.idempotency_key")
        if self.created_at is not None:
            require_iso_datetime(self.created_at, "Receipt.created_at")
        optional_str(self.external_id, "Receipt.external_id", max_len=200)
        require_int(self.schema_version, "Receipt.schema_version", minimum=1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "approval_id": self.approval_id,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
            "external_id": self.external_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Receipt":
        source = require_mapping(data, "Receipt")
        return cls(
            receipt_id=require_str(source.get("receipt_id"), "Receipt.receipt_id", max_len=128),
            approval_id=require_str(source.get("approval_id"), "Receipt.approval_id", max_len=128),
            call_id=require_str(source.get("call_id"), "Receipt.call_id", max_len=128),
            tool_name=require_str(source.get("tool_name"), "Receipt.tool_name", max_len=128),
            status=require_choice(source.get("status"), "Receipt.status", RECEIPT_STATUSES),
            idempotency_key=require_hash_prefix(source.get("idempotency_key"), "Receipt.idempotency_key"),
            created_at=optional_str(source.get("created_at"), "Receipt.created_at"),
            external_id=optional_str(source.get("external_id"), "Receipt.external_id", max_len=200),
            schema_version=require_int(source.get("schema_version"), "Receipt.schema_version", minimum=1),
        )


RECEIPT_JSON_SCHEMA = _object_schema(
    "pq_agent.receipt",
    {
        "receipt_id": {"type": "string", "minLength": 1},
        "approval_id": {"type": "string", "minLength": 1},
        "call_id": {"type": "string", "minLength": 1},
        "tool_name": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": list(RECEIPT_STATUSES)},
        "idempotency_key": {"type": "string", "minLength": 6},
        "created_at": {"type": ["string", "null"]},
        "external_id": {"type": ["string", "null"]},
        "schema_version": _SCHEMA_VERSION_PROPERTY,
    },
    (
        "receipt_id",
        "approval_id",
        "call_id",
        "tool_name",
        "status",
        "idempotency_key",
        "created_at",
        "external_id",
        "schema_version",
    ),
)


# --------------------------------------------------------------------------
# 6. Error
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Error:
    """节点级错误记录（用于 trace/审计，不存异常原文）。"""

    node: str
    error_code: str
    error_type: str
    message: str = ""
    retryable: bool = False
    timestamp: str | None = None
    schema_version: int = ERROR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_str(self.node, "Error.node", max_len=128)
        require_str(self.error_code, "Error.error_code", max_len=128)
        require_str(self.error_type, "Error.error_type", max_len=128)
        require_str(self.message, "Error.message", allow_empty=True, max_len=500)
        require_bool(self.retryable, "Error.retryable")
        if self.timestamp is not None:
            require_iso_datetime(self.timestamp, "Error.timestamp")
        require_int(self.schema_version, "Error.schema_version", minimum=1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "error_code": self.error_code,
            "error_type": self.error_type,
            "message": self.message,
            "retryable": self.retryable,
            "timestamp": self.timestamp,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Error":
        source = require_mapping(data, "Error")
        return cls(
            node=require_str(source.get("node"), "Error.node", max_len=128),
            error_code=require_str(source.get("error_code"), "Error.error_code", max_len=128),
            error_type=require_str(source.get("error_type"), "Error.error_type", max_len=128),
            message=require_str(source.get("message", ""), "Error.message", allow_empty=True, max_len=500),
            retryable=require_bool(source.get("retryable", False), "Error.retryable"),
            timestamp=optional_str(source.get("timestamp"), "Error.timestamp"),
            schema_version=require_int(source.get("schema_version"), "Error.schema_version", minimum=1),
        )


ERROR_JSON_SCHEMA = _object_schema(
    "pq_agent.error",
    {
        "node": {"type": "string", "minLength": 1},
        "error_code": {"type": "string", "minLength": 1},
        "error_type": {"type": "string", "minLength": 1},
        "message": {"type": "string"},
        "retryable": {"type": "boolean"},
        "timestamp": {"type": ["string", "null"]},
        "schema_version": _SCHEMA_VERSION_PROPERTY,
    },
    ("node", "error_code", "error_type", "message", "retryable", "timestamp", "schema_version"),
)


# --------------------------------------------------------------------------
# 7. DataQuality
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DataQuality:
    """数据溯源与质量。live/replay 的差别必须在这里说清楚，不能靠调用方猜。"""

    profile: str
    source: str
    field_quality: Mapping[str, str] = field(default_factory=dict)
    as_of: str | None = None
    fetched_at: str | None = None
    timezone: str = "Asia/Shanghai"
    stale: bool = False
    degraded: bool = False
    data_snapshot_id: str = ""
    schema_version: int = DATA_QUALITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_str(self.profile, "DataQuality.profile", max_len=32)
        require_str(self.source, "DataQuality.source", max_len=200)
        quality = require_json_value(self.field_quality, "DataQuality.field_quality")
        if not isinstance(quality, Mapping):
            raise ContractError("TYPE_INVALID", "DataQuality.field_quality 需要对象")
        for key, value in quality.items():
            if not isinstance(value, str):
                raise ContractError("TYPE_INVALID", f"DataQuality.field_quality.{key} 需要字符串")
        object.__setattr__(self, "field_quality", dict(quality))
        if self.as_of is not None:
            text = require_str(self.as_of, "DataQuality.as_of")
            try:
                dt.date.fromisoformat(text)
            except ValueError as exc:
                raise ContractError("DATE_INVALID", f"DataQuality.as_of='{text}'：{exc}") from exc
        if self.fetched_at is not None:
            require_iso_datetime(self.fetched_at, "DataQuality.fetched_at")
        require_str(self.timezone, "DataQuality.timezone", max_len=64)
        require_bool(self.stale, "DataQuality.stale")
        require_bool(self.degraded, "DataQuality.degraded")
        require_str(self.data_snapshot_id, "DataQuality.data_snapshot_id", allow_empty=True, max_len=200)
        require_int(self.schema_version, "DataQuality.schema_version", minimum=1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "source": self.source,
            "field_quality": dict(self.field_quality),
            "as_of": self.as_of,
            "fetched_at": self.fetched_at,
            "timezone": self.timezone,
            "stale": self.stale,
            "degraded": self.degraded,
            "data_snapshot_id": self.data_snapshot_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DataQuality":
        source = require_mapping(data, "DataQuality")
        return cls(
            profile=require_str(source.get("profile"), "DataQuality.profile", max_len=32),
            source=require_str(source.get("source"), "DataQuality.source", max_len=200),
            field_quality=_opt_mapping(source.get("field_quality"), "DataQuality.field_quality"),
            as_of=optional_str(source.get("as_of"), "DataQuality.as_of"),
            fetched_at=optional_str(source.get("fetched_at"), "DataQuality.fetched_at"),
            timezone=require_str(source.get("timezone", "Asia/Shanghai"), "DataQuality.timezone", max_len=64),
            stale=require_bool(source.get("stale", False), "DataQuality.stale"),
            degraded=require_bool(source.get("degraded", False), "DataQuality.degraded"),
            data_snapshot_id=require_str(
                source.get("data_snapshot_id", ""), "DataQuality.data_snapshot_id", allow_empty=True, max_len=200
            ),
            schema_version=require_int(source.get("schema_version"), "DataQuality.schema_version", minimum=1),
        )


DATA_QUALITY_JSON_SCHEMA = _object_schema(
    "pq_agent.data_quality",
    {
        "profile": {"type": "string", "minLength": 1},
        "source": {"type": "string", "minLength": 1},
        "field_quality": {"type": "object"},
        "as_of": {"type": ["string", "null"]},
        "fetched_at": {"type": ["string", "null"]},
        "timezone": {"type": "string", "minLength": 1},
        "stale": {"type": "boolean"},
        "degraded": {"type": "boolean"},
        "data_snapshot_id": {"type": "string"},
        "schema_version": _SCHEMA_VERSION_PROPERTY,
    },
    (
        "profile",
        "source",
        "field_quality",
        "as_of",
        "fetched_at",
        "timezone",
        "stale",
        "degraded",
        "data_snapshot_id",
        "schema_version",
    ),
)


# --------------------------------------------------------------------------
# 8. Budget
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Budget:
    """预算闸门。超限要返回明确原因，不静默截断。"""

    max_steps: int = 12
    max_tokens: int = 60000
    max_wall_clock_s: float = 120.0
    steps: int = 0
    tokens: int = 0
    started_at: str | None = None
    schema_version: int = BUDGET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_int(self.max_steps, "Budget.max_steps", minimum=1)
        require_int(self.max_tokens, "Budget.max_tokens", minimum=1)
        require_number(self.max_wall_clock_s, "Budget.max_wall_clock_s", minimum=0.1)
        require_int(self.steps, "Budget.steps", minimum=0)
        require_int(self.tokens, "Budget.tokens", minimum=0)
        if self.started_at is not None:
            require_iso_datetime(self.started_at, "Budget.started_at")
        require_int(self.schema_version, "Budget.schema_version", minimum=1)

    def evaluate(self, now: dt.datetime, *, extra_steps: int = 0, extra_tokens: int = 0) -> dict[str, Any]:
        """返回 {ok, code, reason, steps, tokens, elapsed_s}；code 为 None 表示未超限。"""
        if now.tzinfo is None:
            raise ContractError("DATETIME_NAIVE", "Budget.evaluate 的 now 必须带时区")
        steps = self.steps + extra_steps
        tokens = self.tokens + extra_tokens
        elapsed = None
        if self.started_at is not None:
            elapsed = (now - require_iso_datetime(self.started_at, "Budget.started_at")).total_seconds()
        code = None
        reason = ""
        if steps > self.max_steps:
            code, reason = "BUDGET_EXCEEDED", f"steps {steps} > {self.max_steps}"
        elif tokens > self.max_tokens:
            code, reason = "BUDGET_EXCEEDED", f"tokens {tokens} > {self.max_tokens}"
        elif elapsed is not None and elapsed > self.max_wall_clock_s:
            code, reason = "BUDGET_EXCEEDED", f"elapsed_s {elapsed:.3f} > {self.max_wall_clock_s}"
        return {
            "ok": code is None,
            "code": code,
            "reason": reason,
            "steps": steps,
            "tokens": tokens,
            "elapsed_s": elapsed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_tokens": self.max_tokens,
            "max_wall_clock_s": self.max_wall_clock_s,
            "steps": self.steps,
            "tokens": self.tokens,
            "started_at": self.started_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Budget":
        source = require_mapping(data, "Budget")
        return cls(
            max_steps=require_int(source.get("max_steps", 12), "Budget.max_steps", minimum=1),
            max_tokens=require_int(source.get("max_tokens", 60000), "Budget.max_tokens", minimum=1),
            max_wall_clock_s=require_number(source.get("max_wall_clock_s", 120.0), "Budget.max_wall_clock_s", minimum=0.1),
            steps=require_int(source.get("steps", 0), "Budget.steps", minimum=0),
            tokens=require_int(source.get("tokens", 0), "Budget.tokens", minimum=0),
            started_at=optional_str(source.get("started_at"), "Budget.started_at"),
            schema_version=require_int(source.get("schema_version"), "Budget.schema_version", minimum=1),
        )


BUDGET_JSON_SCHEMA = _object_schema(
    "pq_agent.budget",
    {
        "max_steps": {"type": "integer", "minimum": 1},
        "max_tokens": {"type": "integer", "minimum": 1},
        "max_wall_clock_s": {"type": "number", "minimum": 0.1},
        "steps": {"type": "integer", "minimum": 0},
        "tokens": {"type": "integer", "minimum": 0},
        "started_at": {"type": ["string", "null"]},
        "schema_version": _SCHEMA_VERSION_PROPERTY,
    },
    ("max_steps", "max_tokens", "max_wall_clock_s", "steps", "tokens", "started_at", "schema_version"),
)


# --------------------------------------------------------------------------
# 9. Message（state.messages 的唯一合法形状）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """state.messages 的条目：只允许脱敏摘要与 ID，禁止完整 prompt / headers / 异常堆栈。"""

    message_id: str
    role: str
    summary: str
    schema_version: int = MESSAGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_str(self.message_id, "Message.message_id", max_len=128)
        require_choice(self.role, "Message.role", MESSAGE_ROLES)
        require_str(self.summary, "Message.summary", allow_empty=True, max_len=2000)
        require_int(self.schema_version, "Message.schema_version", minimum=1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role,
            "summary": self.summary,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Message":
        source = require_mapping(data, "Message")
        extra = set(source) - {"message_id", "role", "summary", "schema_version"}
        if extra:
            raise ContractError(
                "MESSAGE_FORBIDDEN_FIELD",
                f"Message 不允许字段 {sorted(extra)}；state 只保存脱敏摘要与 ID",
            )
        return cls(
            message_id=require_str(source.get("message_id"), "Message.message_id", max_len=128),
            role=require_choice(source.get("role"), "Message.role", MESSAGE_ROLES),
            summary=require_str(source.get("summary", ""), "Message.summary", allow_empty=True, max_len=2000),
            schema_version=require_int(source.get("schema_version"), "Message.schema_version", minimum=1),
        )


MESSAGE_JSON_SCHEMA = _object_schema(
    "pq_agent.message",
    {
        "message_id": {"type": "string", "minLength": 1},
        "role": {"type": "string", "enum": list(MESSAGE_ROLES)},
        "summary": {"type": "string"},
        "schema_version": _SCHEMA_VERSION_PROPERTY,
    },
    ("message_id", "role", "summary", "schema_version"),
)


JSON_SCHEMAS: dict[str, dict[str, Any]] = {
    "message": MESSAGE_JSON_SCHEMA,
    "tool_call": TOOL_CALL_JSON_SCHEMA,
    "tool_result": TOOL_RESULT_JSON_SCHEMA,
    "citation": CITATION_JSON_SCHEMA,
    "approval": APPROVAL_JSON_SCHEMA,
    "receipt": RECEIPT_JSON_SCHEMA,
    "error": ERROR_JSON_SCHEMA,
    "data_quality": DATA_QUALITY_JSON_SCHEMA,
    "budget": BUDGET_JSON_SCHEMA,
}

__all__ = [
    "APPROVAL_JSON_SCHEMA",
    "APPROVAL_STATUSES",
    "Approval",
    "BUDGET_JSON_SCHEMA",
    "Budget",
    "CAPABILITIES",
    "CITATION_JSON_SCHEMA",
    "CONTRACTS_SCHEMA_VERSION",
    "CONTRACT_SCHEMA_VERSIONS",
    "Citation",
    "ContractError",
    "DATA_QUALITY_JSON_SCHEMA",
    "DataQuality",
    "ERROR_JSON_SCHEMA",
    "Error",
    "INTENT_SOURCES",
    "JSON_SCHEMAS",
    "MESSAGE_JSON_SCHEMA",
    "MESSAGE_ROLES",
    "MESSAGE_SCHEMA_VERSION",
    "Message",
    "RECEIPT_JSON_SCHEMA",
    "RECEIPT_STATUSES",
    "Receipt",
    "TOOL_CALL_JSON_SCHEMA",
    "TOOL_RESULT_JSON_SCHEMA",
    "ToolCall",
    "ToolResult",
    "iso",
    "optional_str",
    "require_bool",
    "require_choice",
    "require_hash64",
    "require_hash_prefix",
    "require_int",
    "require_iso_datetime",
    "require_json_value",
    "require_mapping",
    "require_number",
    "require_relative_posix_path",
    "require_sequence",
    "require_str",
    "validate_against_schema",
]
