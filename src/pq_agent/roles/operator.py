"""operator：只做审批后的写入与外发，不自己取数、不自己判断该不该做。

三条不能让步的规矩：
1. operator 拿不到只读工具：它要写什么，必须由上游（analyst 的证据 + supervisor 的路由）
   决定。让 operator 自己取数，就有了「自己取数 → 自己决定 → 自己写」的闭环，
   人工确认会退化成盖橡皮图章。
2. 有能力的工具不等于能执行：operator 的每次写入仍走 graph 的 request_approval + outbox，
   审批 token 必须等于 registry 自己清洗后算出的 arguments_hash。
3. 未审批时零副作用：本模块不提供任何「跳过审批」的入口，也不缓存审批结果。
"""

from __future__ import annotations

from typing import Any

from pq_agent.roles.contracts import (
    ROLE_ERROR_CAPABILITY,
    ROLE_OPERATOR,
    RoleError,
    allowed_tool_names,
    reject_reason,
    role_definition,
    visible_schemas,
)

# operator 的返回形状：只有回执，没有内容。
# 它不该把写入的正文回吐给上游——正文属于业务文件，不属于角色交接。
RETURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string"},
        "approval_id": {"type": "string"},
        "arguments_hash": {"type": "string"},
        "status": {"type": "string", "enum": ["SENT", "FAILED", "UNKNOWN"]},
        "targets": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tool", "status"],
    "additionalProperties": False,
}


def require_write_capability(tool_name: str, *, capability: str) -> None:
    """operator 只能碰写/外发能力。其余一律拒绝，并说清是哪个角色的哪类能力。"""

    if str(capability) not in role_definition(ROLE_OPERATOR).capabilities:
        raise RoleError(
            ROLE_ERROR_CAPABILITY,
            reject_reason(ROLE_OPERATOR, tool_name, capability=capability),
        )


def describe(registry: Any = None) -> dict[str, Any]:
    definition = role_definition(ROLE_OPERATOR)
    payload: dict[str, Any] = {
        "role": definition.role,
        "capabilities": list(definition.capabilities),
        "purpose": definition.purpose,
        "requires_approval": True,
        "return_schema": dict(RETURN_SCHEMA),
        "tool_count": 0,
        "tools": [],
        "schema_version": definition.schema_version,
    }
    if registry is not None:
        names = allowed_tool_names(registry, ROLE_OPERATOR)
        payload["tool_count"] = len(names)
        payload["tools"] = names
        payload["schemas"] = visible_schemas(registry, ROLE_OPERATOR)
    return payload


__all__ = ["RETURN_SCHEMA", "require_write_capability", "describe"]
