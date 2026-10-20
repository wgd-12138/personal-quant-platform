"""analyst：只做只读取数与检索，产出证据，不产生任何副作用。

第一条硬规矩：analyst 的工具集合由能力标签筛出来，不是写死名字。
写死名字意味着新加一个只读工具时 analyst 会「看不见」，
那一刻起权限表就和注册表脱节了。

第二条硬规矩：analyst 请求写/外发工具时，这里连 schema 都不给，
同时 run_tool 会再查一次能力并拒绝执行——可见面与执行面必须同时成立。
"""

from __future__ import annotations

from typing import Any

from pq_agent.roles.contracts import (
    ROLE_ANALYST,
    RoleError,
    ROLE_ERROR_CAPABILITY,
    allowed_tool_names,
    context_digest,
    reject_reason,
    role_definition,
    visible_schemas,
)

# analyst 的返回形状：只有证据与统计，没有结论。
# 不给 conclusions / advice 这类字段，是因为本项目的红线是
# 「AI 不能单独生成买卖结论」——结构上就不留位置比事后审核可靠。
RETURN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "evidence": {"type": "array", "items": {"type": "object"}},
        "citations": {"type": "array", "items": {"type": "string"}},
        "data_snapshot_id": {"type": "string"},
        "degraded": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["evidence"],
    "additionalProperties": False,
}


def describe(registry: Any = None) -> dict[str, Any]:
    """analyst 的自我说明。registry 给定时顺便列出它真实可见的工具。"""

    definition = role_definition(ROLE_ANALYST)
    payload: dict[str, Any] = {
        "role": definition.role,
        "capabilities": list(definition.capabilities),
        "purpose": definition.purpose,
        "return_schema": dict(RETURN_SCHEMA),
        "tool_count": 0,
        "tools": [],
        "schema_version": definition.schema_version,
    }
    if registry is not None:
        names = allowed_tool_names(registry, ROLE_ANALYST)
        payload["tool_count"] = len(names)
        payload["tools"] = names
        payload["schemas"] = visible_schemas(registry, ROLE_ANALYST)
    return payload


def require_read_capability(tool_name: str, *, capability: str) -> None:
    """analyst 只能碰只读能力。其余一律拒绝，并说清是哪个角色的哪类能力。"""

    if str(capability) not in role_definition(ROLE_ANALYST).capabilities:
        raise RoleError(
            ROLE_ERROR_CAPABILITY,
            reject_reason(ROLE_ANALYST, tool_name, capability=capability),
        )


def context_digest_of(value: Any) -> str:
    return context_digest(value)


__all__ = ["RETURN_SCHEMA", "describe", "require_read_capability", "context_digest_of"]
