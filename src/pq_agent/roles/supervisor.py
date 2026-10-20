"""supervisor：只做意图识别与路由，不持有任何工具能力。

为什么 supervisor 连只读工具都不给：
一旦它手上能调工具，路由节点就会顺手替 analyst 取数，
「谁读了数据」这件事在 trace 里就分不清了。职责边界靠不给权限来保证，
而不是靠一句「supervisor 请只做路由」的提示词。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pq_agent.roles.contracts import (
    ROLE_ANALYST,
    ROLE_ERROR_CAPABILITY,
    ROLE_OPERATOR,
    ROLE_SUPERVISOR,
    allowed_tool_names,
    capabilities_for,
    role_definition,
    visible_schemas,
)

# 意图 → 承接角色。supervisor 自己不出现在右边：它只负责把活派出去。
INTENT_TO_ROLE: dict[str, str] = {
    "pos_question": ROLE_ANALYST,
    "market_check": ROLE_ANALYST,
    "history_question": ROLE_ANALYST,
    "risk_question": ROLE_ANALYST,
    "report_generate": ROLE_OPERATOR,
    "push_request": ROLE_OPERATOR,
    "code_task": "",
    "fallback": "",
}


def route_role(intent: str) -> str:
    """把意图映射到承接角色。

    code_task 与 fallback 返回空串表示「没有角色承接」：
    宁可明说没人接，也不要随便塞给 analyst 让它在无权限的情况下空转。
    """

    key = str(intent or "").strip()
    if key not in INTENT_TO_ROLE:
        return ""
    return INTENT_TO_ROLE[key]


def describe(registry: Any = None) -> dict[str, Any]:
    """supervisor 的自我说明。registry 只用于展示它确实一个工具都看不到。"""

    definition = role_definition(ROLE_SUPERVISOR)
    payload: dict[str, Any] = {
        "role": definition.role,
        "capabilities": list(definition.capabilities),
        "purpose": definition.purpose,
        "intents": sorted(INTENT_TO_ROLE),
        "routes": {key: value for key, value in sorted(INTENT_TO_ROLE.items())},
        "tool_count": 0,
        "tools": [],
        "schema_version": definition.schema_version,
    }
    if registry is not None:
        names = allowed_tool_names(registry, ROLE_SUPERVISOR)
        payload["tools"] = names
        payload["tool_count"] = len(names)
        payload["schemas"] = visible_schemas(registry, ROLE_SUPERVISOR)
        if names:
            raise RoleError(
                ROLE_ERROR_CAPABILITY,
                "supervisor 不应该看到任何工具：" + ", ".join(names),
            )
    return payload


__all__ = ["INTENT_TO_ROLE", "route_role", "describe"]
