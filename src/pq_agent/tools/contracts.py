"""
工具合同：每个工具必须声明能力、副作用、参数 schema、超时和幂等策略。

为什么要有这份合同：
- 权限判定必须能从代码里读出来，而不是从提示词里猜出来。
- 一次工具调用要不要人工确认，取决于 capability 和 side_effects，不取决于模型怎么说。
- 返回值必须结构化（ToolResult 或 ToolError），不能把异常文本直接丢给模型。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pq_agent.contracts import CAPABILITIES

# 结果负载统一截断长度：模型不需要看完整报告，只需要看结论和证据位置
PAYLOAD_MAX_CHARS = 4000
PAYLOAD_MAX_ITEMS = 50
TEXT_MAX_CHARS = 500

# 风险等级：描述做错了会怎样，用于排序和审计，不用于代替 capability 判定
RISK_LEVELS: tuple[str, ...] = ("none", "low", "medium", "high")

# 工具层错误码（与 graph 层错误码分开，便于区分调用被拒还是执行失败）
TOOL_ERROR_CODES: tuple[str, ...] = (
    "TOOL_NOT_ALLOWED",
    "TOOL_FIELD_FORBIDDEN",
    "TOOL_SCHEMA_INVALID",
    "TOOL_LOOP_COMMAND_REJECTED",
    "TOOL_APPROVAL_REQUIRED",
    "TOOL_TRADING_LAYER_REJECTED",
    "TOOL_READ_ONLY_VIOLATION",
    "TOOL_TIMEOUT",
    "TOOL_EXECUTION_FAILED",
    # 沙箱拒绝/不可用与「工具本身执行失败」必须分开：前者是安全边界生效，
    # 后者是工具坏了。混成一个码会让「今天沙箱拒了 12 次」看起来像「工具不稳定」。
    "TOOL_SANDBOX_BLOCKED",
    "INVALID_AS_OF",
    "INVALID_SLOT",
    "INPUTS_NOT_READY",
    "DATA_SOURCE_UNAVAILABLE",
)


class ToolError(ValueError):
    """工具调用的结构化失败。code 供程序判断，message 给人看，hint 给下一步建议。"""

    def __init__(self, code: str, message: str, *, hint: str = "", details: Mapping[str, Any] | None = None):
        if code not in TOOL_ERROR_CODES:
            raise ValueError(f"unknown tool error code: {code}")
        self.code = code
        self.message = str(message)[:TEXT_MAX_CHARS]
        self.hint = str(hint)[:TEXT_MAX_CHARS]
        self.details = dict(details or {})
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
            "hint": self.hint,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class ArgSpec:
    """单个参数的白名单声明。"""

    name: str
    type: str  # string | integer | boolean | enum
    required: bool = False
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.type not in ("string", "integer", "boolean", "enum"):
            raise ValueError(f"unknown arg type {self.type} for {self.name}")
        if self.type == "enum" and not self.choices:
            raise ValueError(f"enum arg {self.name} needs choices")

    def to_json_schema(self) -> dict[str, Any]:
        if self.type == "enum":
            schema: dict[str, Any] = {"type": "string", "enum": list(self.choices)}
        else:
            schema = {"type": self.type}
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.description:
            schema["description"] = self.description
        return schema


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整合同。registry 只接受声明过的字段，其余一律拒绝。"""

    name: str
    capability: str
    side_effects: tuple[str, ...]
    args: tuple[ArgSpec, ...]
    returns: str
    timeout_s: float
    idempotency: str  # safe | requires_approval | non_retryable
    risk: str
    summary: str
    cli_command: str = ""
    requires_approval: bool = False
    writes_business_files: bool = False
    notifies_external: bool = False
    reads_network: bool = False
    handler: Callable[..., Any] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.capability not in CAPABILITIES and self.capability != "code_exec":
            raise ValueError(f"{self.name}: unknown capability {self.capability}")
        if self.risk not in RISK_LEVELS:
            raise ValueError(f"{self.name}: unknown risk {self.risk}")
        if self.idempotency not in ("safe", "requires_approval", "non_retryable"):
            raise ValueError(f"{self.name}: unknown idempotency {self.idempotency}")
        if self.timeout_s <= 0:
            raise ValueError(f"{self.name}: timeout_s must be positive")
        if not self.name or self.name != self.name.strip():
            raise ValueError("tool name must be non-empty and trimmed")
        seen: set[str] = set()
        for spec in self.args:
            if spec.name in seen:
                raise ValueError(f"{self.name}: duplicate arg {spec.name}")
            seen.add(spec.name)
        # 有副作用就必须走人工确认，这条是硬绑定，不允许单独关掉
        if self.side_effects and not self.requires_approval:
            raise ValueError(f"{self.name}: side effects require approval")
        # 声明了外发能力的工具必须同时声明副作用，避免漏掉确认
        if self.notifies_external and not self.side_effects:
            raise ValueError(f"{self.name}: external notify must declare side effects")

    @property
    def arg_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.args)

    @property
    def required_arg_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.args if spec.required)

    @property
    def is_read_only(self) -> bool:
        return self.capability in ("read_local", "read_external") and not self.side_effects

    def args_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {spec.name: spec.to_json_schema() for spec in self.args},
            "required": list(self.required_arg_names),
            "additionalProperties": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "capability": self.capability,
            "side_effects": list(self.side_effects),
            "risk": self.risk,
            "idempotency": self.idempotency,
            "timeout_s": self.timeout_s,
            "requires_approval": self.requires_approval,
            "cli_command": self.cli_command,
            "reads_network": self.reads_network,
            "writes_business_files": self.writes_business_files,
            "notifies_external": self.notifies_external,
            "summary": self.summary,
            "args_schema": self.args_schema(),
            "returns": self.returns,
        }


def truncate_payload(value: Any, *, max_chars: int = PAYLOAD_MAX_CHARS, max_items: int = PAYLOAD_MAX_ITEMS) -> tuple[Any, bool]:
    """把工具返回值裁到模型能消化的规模，并如实报告是否发生了截断。"""
    truncated = False

    def _walk(node: Any, depth: int) -> Any:
        nonlocal truncated
        if depth > 6:
            truncated = True
            return "<depth-limit>"
        if isinstance(node, str):
            if len(node) > max_chars:
                truncated = True
                return node[:max_chars] + f"...<truncated {len(node) - max_chars} chars>"
            return node
        if isinstance(node, Mapping):
            out: dict[str, Any] = {}
            for index, (key, item) in enumerate(node.items()):
                if index >= max_items:
                    truncated = True
                    out["__truncated_keys__"] = len(node) - max_items
                    break
                out[str(key)] = _walk(item, depth + 1)
            return out
        if isinstance(node, (list, tuple)):
            if len(node) > max_items:
                truncated = True
                walked = [_walk(item, depth + 1) for item in node[:max_items]]
                walked.append(f"<truncated {len(node) - max_items} items>")
                return walked
            return [_walk(item, depth + 1) for item in node]
        if isinstance(node, bool) or node is None or isinstance(node, (int, float)):
            return node
        return str(node)[:TEXT_MAX_CHARS]

    return _walk(value, 0), truncated


def _digest(text: str) -> str:
    from pq_agent.data.models import sha256_text

    return sha256_text(text)


def canonical_arguments(arguments: Mapping[str, Any], *, names: Iterable[str] | None = None) -> str:
    """规范化参数用于审批绑定：只保留白名单字段，键排序，中文不转义。"""
    allowed = set(names) if names is not None else None
    cleaned = {
        str(key): value
        for key, value in arguments.items()
        if allowed is None or str(key) in allowed
    }
    return json.dumps(cleaned, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def arguments_hash(tool_name: str, arguments: Mapping[str, Any], *, names: Iterable[str] | None = None) -> str:
    """审批摘要：由工具名与参数一起算，防止批准 A 却执行 B。"""
    body = canonical_arguments(arguments, names=names)
    return _digest(tool_name + "\n" + body)


def build_result(
    spec: ToolSpec,
    *,
    data: Mapping[str, Any] | None = None,
    summary: str = "",
    artifacts: Sequence[Mapping[str, Any]] = (),
    warnings: Sequence[str] = (),
    degraded: Sequence[str] = (),
    duration_ms: float | None = None,
    data_source: str = "",
    dry_run: bool = True,
    as_of: str | None = None,
    slot: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造统一结果。字段与 A0-02 的 6.3 结果 schema 对齐，便于日志与评测比对。"""
    payload, truncated = truncate_payload(dict(data or {}))
    body: dict[str, Any] = {
        "ok": True,
        "error_code": None,
        "tool": spec.name,
        "command": spec.cli_command,
        "capability": spec.capability,
        "side_effects": list(spec.side_effects),
        "as_of": as_of,
        "slot": slot,
        "dry_run": bool(dry_run),
        "data_source": data_source,
        "summary": str(summary)[:TEXT_MAX_CHARS],
        "data": payload,
        "truncated": truncated,
        "degraded": list(degraded),
        "artifacts": [dict(item) for item in artifacts],
        "warnings": [str(item)[:TEXT_MAX_CHARS] for item in warnings],
        "duration_ms": duration_ms,
    }
    if extra:
        body.update(dict(extra))
    return body
