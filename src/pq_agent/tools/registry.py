"""工具注册表：deny-by-default，权限判定在代码层完成。

设计要点：
1. 注册表是唯一入口，未登记的工具名一律拒绝，绝不回落到 shell 或 getattr 动态调用。
2. 校验顺序固定：字段白名单 -> 参数 schema -> 交易层前缀 -> 审批 -> 执行。
3. 交易层（broker/execution/risk/order/trade）前缀直接被拒，不管有没有注册。
4. 只读工具不写业务文件；写盘与外发必须带与参数绑定的 approval_token。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pq_agent.tools.contracts import (
    TOOL_ERROR_CODES,
    ToolError,
    ToolSpec,
    arguments_hash,
)

TRADING_LAYER_PREFIXES = ("broker_", "execution_", "risk_", "order_", "trade_")
LOOP_HINTS = ("monitor", "watch", "loop")


def _reject_trading_layer(name: str) -> None:
    for prefix in TRADING_LAYER_PREFIXES:
        if name.startswith(prefix):
            raise ToolError(
                "TOOL_TRADING_LAYER_REJECTED",
                f"拒绝交易层工具：{name}",
                hint="代理不得触碰券商、执行与风控模块，请改用只读分析工具。",
            )


def _validate_arguments(spec: ToolSpec, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """按 schema 校验参数：未知字段、缺失必填、类型与取值全都在这里拦。"""
    if not isinstance(arguments, Mapping):
        raise ToolError("TOOL_SCHEMA_INVALID", "arguments 必须是字典")
    allowed = set(spec.arg_names)
    unknown = sorted(set(str(k) for k in arguments) - allowed)
    if unknown:
        joined = ", ".join(unknown)
        allow_text = ", ".join(sorted(allowed)) or "（无）"
        raise ToolError(
            "TOOL_FIELD_FORBIDDEN",
            f"{spec.name} 不接受字段：{joined}",
            hint=f"允许的字段只有：{allow_text}。",
        )
    missing = [item for item in spec.required_arg_names if item not in arguments]
    if missing:
        raise ToolError(
            "TOOL_SCHEMA_INVALID",
            f"{spec.name} 缺少必填字段：{', '.join(missing)}",
        )
    cleaned: dict[str, Any] = {}
    for item in spec.args:
        if item.name not in arguments:
            continue
        value = arguments[item.name]
        if item.type == "string":
            if not isinstance(value, str):
                raise ToolError("TOOL_SCHEMA_INVALID", f"{item.name} 必须是字符串")
        elif item.type == "boolean":
            if not isinstance(value, bool):
                raise ToolError("TOOL_SCHEMA_INVALID", f"{item.name} 必须是布尔值")
        elif item.type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError("TOOL_SCHEMA_INVALID", f"{item.name} 必须是整数")
            if item.minimum is not None and value < item.minimum:
                raise ToolError("TOOL_SCHEMA_INVALID", f"{item.name} 不得小于 {item.minimum}")
            if item.maximum is not None and value > item.maximum:
                raise ToolError("TOOL_SCHEMA_INVALID", f"{item.name} 不得大于 {item.maximum}")
        if item.choices and value not in item.choices:
            options = ", ".join(map(str, item.choices))
            raise ToolError(
                "TOOL_SCHEMA_INVALID",
                f"{item.name} 取值非法：{value!r}",
                hint=f"可选值：{options}。",
            )
        cleaned[item.name] = value
    return cleaned


def _check_loop_command(spec: ToolSpec) -> None:
    """循环命令禁止注册：超时后被强杀会留下半个状态文件，谁也说不清写到哪一步。"""
    if spec.cli_command.endswith("-once"):
        return
    for hint in LOOP_HINTS:
        if hint in spec.name or hint in spec.cli_command:
            raise ToolError(
                "TOOL_LOOP_COMMAND_REJECTED",
                f"拒绝注册循环工具：{spec.name}",
                hint="循环命令只能暴露 *-once 变体。",
            )


@dataclass
class ToolRegistry:
    """工具注册表。默认拒绝一切未登记工具。"""

    _specs: dict[str, ToolSpec] = field(default_factory=dict)
    call_log: list[dict[str, Any]] = field(default_factory=list)

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.handler is None:
            raise ToolError("TOOL_SCHEMA_INVALID", f"{spec.name} 没有绑定 handler")
        _reject_trading_layer(spec.name)
        _check_loop_command(spec)
        if spec.name in self._specs:
            raise ToolError("TOOL_SCHEMA_INVALID", f"工具重复注册：{spec.name}")
        self._specs[spec.name] = spec
        return spec

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[ToolSpec]:
        return [self._specs[name] for name in self.names()]

    def get(self, name: str) -> ToolSpec:
        _reject_trading_layer(str(name))
        spec = self._specs.get(str(name))
        if spec is None:
            raise ToolError(
                "TOOL_NOT_ALLOWED",
                f"未登记的工具：{name}",
                hint="只允许登记过的工具，不做前缀匹配或模糊纠错。",
            )
        return spec

    def schemas(self) -> list[dict[str, Any]]:
        return [spec.to_dict() for spec in self.specs()]

    def preview(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """只校验不执行，返回审批摘要，供 HITL 节点展示给用户。"""
        spec = self.get(name)
        cleaned = _validate_arguments(spec, arguments or {})
        return {
            "tool": spec.name,
            "capability": spec.capability,
            "side_effects": list(spec.side_effects),
            "risk": spec.risk,
            "requires_approval": bool(spec.requires_approval or spec.side_effects),
            "arguments": cleaned,
            "arguments_hash": arguments_hash(spec.name, cleaned, names=spec.arg_names),
        }

    def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        approval_token: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """执行工具。所有校验必须在真正调用 handler 之前完成。"""
        spec = self.get(name)
        cleaned = _validate_arguments(spec, arguments or {})
        digest = arguments_hash(spec.name, cleaned, names=spec.arg_names)
        needs_approval = bool(spec.requires_approval or spec.side_effects)
        if needs_approval:
            if not approval_token:
                raise ToolError(
                    "TOOL_APPROVAL_REQUIRED",
                    f"{spec.name} 需要人工确认后才能执行",
                    hint="请先取得与该工具及参数绑定的 approval_token。",
                    details={"arguments_hash": digest},
                )
            if approval_token != digest:
                raise ToolError(
                    "TOOL_APPROVAL_REQUIRED",
                    f"{spec.name} 的审批与实际参数不匹配",
                    hint="防止批准 A 却执行 B：请对当前参数重新确认。",
                    details={"arguments_hash": digest},
                )
        self.call_log.append({
            "tool": spec.name,
            "capability": spec.capability,
            "side_effects": list(spec.side_effects),
            "risk": spec.risk,
            "dry_run": bool(dry_run),
            "arguments_hash": digest,
        })
        return spec.handler(cleaned, dry_run=dry_run)
