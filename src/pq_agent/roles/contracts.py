"""角色契约：把「谁可以用哪些工具」写成代码里的常量与检查，而不是写在提示词里。

设计出发点（对应 A5-01 的 done：权限差异由代码和测试证明，不靠 prompt 告诫）：
1. 角色的区别只有一件事：能看到的工具 schema 集合不同，能执行的能力不同。
2. 工具 schema 是「可见面」，能力标签是「执行面」，两面必须同源，否则会出现
   「schema 里看不到、执行却没被拦住」这种只防君子不防小人的隔离。
3. handoff 只传摘要与结构，不传原文：多角色之间传全文会把 prompt 隔离和脱敏一次性作废。
4. 角色不是权限凭证。operator 拿到写工具，仍必须走审批与出件箱；
   本模块只回答「这个角色能不能碰这类工具」，不回答「这次动作被批准了吗」。

刻意不做的事：不做 swarm、不做自组织、不做并发角色协商。
本场景是单域单用户，多角色的收益只在「权限面收窄 + 责任可解释」，
引入并发协商只会增加调试成本和不可复现性。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ROLE_SCHEMA_VERSION",
    "ROLE_SUPERVISOR",
    "ROLE_ANALYST",
    "ROLE_OPERATOR",
    "ROLES",
    "ROLE_CAPABILITIES",
    "ROLE_PURPOSE",
    "HANDOFF_SCHEMA_VERSION",
    "HANDOFF_MAX_TASK_CHARS",
    "HANDOFF_MAX_DIGEST_CHARS",
    "HANDOFF_MAX_TOOLS",
    "HANDOFF_MAX_SCHEMA_CHARS",
    "ROLE_ERROR_UNKNOWN",
    "ROLE_ERROR_CAPABILITY",
    "ROLE_CAPABILITY_DENIED_CODE",
    "ROLE_ERROR_HANDOFF",
    "ROLE_ERROR_FIELD",
    "RoleError",
    "RoleDefinition",
    "role_definition",
    "capabilities_for",
    "may_use_capability",
    "allowed_tool_names",
    "visible_schemas",
    "context_digest",
    "HandoffReceipt",
    "Handoff",
    "build_handoff",
    "reject_reason",
    "describe_roles",
]
# 角色标识。字符串就是协议的一部分：写进日志、trace 属性和回执里，不允许自由发挥。
ROLE_SCHEMA_VERSION = "pq_agent.roles/1"
ROLE_SUPERVISOR = "supervisor"
ROLE_ANALYST = "analyst"
ROLE_OPERATOR = "operator"
ROLES = (ROLE_SUPERVISOR, ROLE_ANALYST, ROLE_OPERATOR)

# 每个角色能碰的能力集合。
# supervisor 的能力集是空的：它的职责是意图与路由，路由不调用工具，
# 所以它连只读工具都不需要——给它只读权限只会让它有机会越权替 analyst 取数。
ROLE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    ROLE_SUPERVISOR: (),
    ROLE_ANALYST: ("read_local", "read_external"),
    ROLE_OPERATOR: ("business_write", "notify_external"),
}

ROLE_PURPOSE: dict[str, str] = {
    ROLE_SUPERVISOR: "只做意图识别与路由决策，不持有任何工具能力",
    ROLE_ANALYST: "只做只读取数与检索，产出证据，不产生任何副作用",
    ROLE_OPERATOR: "只做审批后的写入与外发，不自己取数、不自己判断该不该做",
}

# handoff 的体积上限。这些数字不是拍脑袋：它们要保证「换一个角色接手」
# 时不可能顺带把整段对话或整份持仓搬过去。
HANDOFF_SCHEMA_VERSION = "pq_agent.role_handoff/1"
HANDOFF_MAX_TASK_CHARS = 200
HANDOFF_MAX_DIGEST_CHARS = 64
HANDOFF_MAX_TOOLS = 24
HANDOFF_MAX_SCHEMA_CHARS = 4000

ROLE_ERROR_UNKNOWN = "ROLE_UNKNOWN"
ROLE_ERROR_CAPABILITY = "ROLE_CAPABILITY_DENIED"
# 同一个码的对外别名。测试与调用方读这个更有意义：它描述的是「被拒」这件事本身。
ROLE_CAPABILITY_DENIED_CODE = ROLE_ERROR_CAPABILITY
ROLE_ERROR_HANDOFF = "ROLE_HANDOFF_INVALID"
ROLE_ERROR_FIELD = "ROLE_HANDOFF_FIELD_FORBIDDEN"


class RoleError(RuntimeError):
    """角色层拒绝。code 固定取自上面的常量，调用方按码分支，不解析文案。"""

    def __init__(self, code: str, detail: str = "", *, hint: str = "") -> None:
        if code not in (ROLE_ERROR_UNKNOWN, ROLE_ERROR_CAPABILITY, ROLE_ERROR_HANDOFF, ROLE_ERROR_FIELD):
            raise ValueError("未知的角色错误码：" + str(code))
        super().__init__(code + ": " + detail)
        self.code = code
        self.detail = detail
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "hint": self.hint}


@dataclass(frozen=True)
class RoleDefinition:
    """一个角色的静态定义。不可变：角色权限不允许在运行期被改。"""

    role: str
    capabilities: tuple[str, ...]
    purpose: str
    schema_version: str = ROLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise RoleError(ROLE_ERROR_UNKNOWN, "未登记的角色：" + str(self.role))
        if not self.purpose:
            raise RoleError(ROLE_ERROR_HANDOFF, "角色 " + str(self.role) + " 缺少职责说明")

    @property
    def read_only(self) -> bool:
        return all(item in ("read_local", "read_external") for item in self.capabilities)

    @property
    def has_side_effects(self) -> bool:
        return any(item in ("control_write", "business_write", "notify_external") for item in self.capabilities)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "capabilities": list(self.capabilities),
            "purpose": self.purpose,
            "read_only": self.read_only,
            "has_side_effects": self.has_side_effects,
            "schema_version": self.schema_version,
        }


def role_definition(role: str) -> RoleDefinition:
    """取角色定义；未登记直接抛错，不做大小写宽容或默认回退。"""

    key = str(role or "").strip()
    if key not in ROLES:
        raise RoleError(
            ROLE_ERROR_UNKNOWN,
            "未登记的角色：" + repr(key),
            hint="只允许 " + ", ".join(ROLES) + " 三个角色，不做模糊匹配。",
        )
    return RoleDefinition(role=key, capabilities=ROLE_CAPABILITIES[key], purpose=ROLE_PURPOSE[key])


def capabilities_for(role: str) -> tuple[str, ...]:
    return role_definition(role).capabilities


def may_use_capability(role: str, capability: str) -> bool:
    """这个角色能不能碰这类能力。能力标签不认识时返回 False（fail-closed）。"""

    return str(capability) in capabilities_for(role)

def allowed_tool_names(registry: Any, role: str) -> list[str]:
    """这个角色能看到的工具名。

    注意它只按「能力标签」筛选，不看工具名白名单：
    写死工具名列表会在新工具上线时静默变成漏网之鱼，
    而能力标签是 ToolSpec 的必填字段，新工具绕不过去。
    """

    caps = set(capabilities_for(role))
    if not caps:
        return []
    names: list[str] = []
    for spec in registry.specs():
        if str(getattr(spec, "capability", "")) in caps:
            names.append(str(spec.name))
    return sorted(names)


def visible_schemas(registry: Any, role: str) -> list[dict[str, Any]]:
    """这个角色能看到的 schema。模型只看得到这里返回的部分。

    这是「可见面」：schema 拿不到的工具，模型连它存在都不知道，
    自然也不会去构造调用。但这不等于执行被拦住——
    执行面由 run_tool 再查一次能力，两面都必须成立（见 A5-01 的 verify 第 1 条）。
    """

    allowed = set(allowed_tool_names(registry, role))
    return [dict(item) for item in registry.schemas() if str(item.get("name") or "") in allowed]


def context_digest(value: Any) -> str:
    """把交给下一个角色的上下文压成一个摘要。

    单向前缀 ck_ 加 sha256 前 32 位：够用且不泄露长度。
    这里不做脱敏，脱敏是安全层的职责（pq_agent.security.redaction），
    本函数只负责「同一个输入必然得到同一个摘要」这条可核对性。
    """

    if isinstance(value, Mapping) or isinstance(value, (list, tuple)):
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    elif value is None:
        payload = ""
    else:
        payload = str(value)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return "ck_" + digest[:32]


@dataclass(frozen=True)
class HandoffReceipt:
    """交接回执：证明「这次交接发生过、带了什么、被谁拒了」。

    它是给人看和给 trace 关联用的，不携带任何原文。
    """

    from_role: str
    to_role: str
    task: str
    context_digest: str
    allowed_tools: tuple[str, ...] = ()
    rejected_tools: tuple[str, ...] = ()
    reject_reason: str = ""
    accepted: bool = True
    trace_id: str = ""
    created_at: str = ""
    schema_version: str = HANDOFF_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_role": self.from_role,
            "to_role": self.to_role,
            "task": self.task,
            "context_digest": self.context_digest,
            "allowed_tools": list(self.allowed_tools),
            "rejected_tools": list(self.rejected_tools),
            "reject_reason": self.reject_reason,
            "accepted": bool(self.accepted),
            "trace_id": self.trace_id,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class Handoff:
    """角色交接包。字段就四个，多的一个都不给。

    刻意不提供 arguments、prompt、positions、messages 这类字段：
    一旦结构上留了位置，某天就会有人往里塞原文，隔离也就名存实亡。
    """

    task: str
    context_digest: str
    allowed_tools: tuple[str, ...] = ()
    return_schema: Mapping[str, Any] = field(default_factory=dict)
    from_role: str = ROLE_SUPERVISOR
    to_role: str = ROLE_ANALYST
    schema_version: str = HANDOFF_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "context_digest": self.context_digest,
            "allowed_tools": list(self.allowed_tools),
            "return_schema": dict(self.return_schema),
            "from_role": self.from_role,
            "to_role": self.to_role,
            "schema_version": self.schema_version,
        }

# handoff 里绝对不允许出现的字段名（规范化后比较：小写 + 只留字母数字）。
# 这份名单比安全层的 SENSITIVE_KEY_NAMES 更宽：后者管「秘密」，
# 这里还要管「原文」——整段对话、持仓正文、任意工具参数都必须拦。
HANDOFF_FORBIDDEN_FIELDS = frozenset(
    {
        "prompt",
        "systemprompt",
        "rawprompt",
        "userquery",
        "query",
        "messages",
        "message",
        "conversation",
        "history",
        "transcript",
        "arguments",
        "args",
        "toolarguments",
        "rawarguments",
        "positions",
        "positiontext",
        "holdings",
        "rawdata",
        "rawtext",
        "report",
        "reporttext",
        "filecontent",
        "checkpoint",
        "state",
        "token",
        "secret",
        "apikey",
        "password",
        "authorization",
        "cookie",
        "webhook",
    }
)


def _normalize_field(name: Any) -> str:
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def _assert_handoff_fields_safe(extra: Mapping[str, Any]) -> None:
    """任何额外的键都要先过这张黑名单。留这道口子是给未来加字段用的，
    但加进来之前必须先证明它不是原文。"""

    for key in (extra or {}):
        if _normalize_field(key) in HANDOFF_FORBIDDEN_FIELDS:
            raise RoleError(
                ROLE_ERROR_FIELD,
                "handoff 不允许携带字段：" + str(key),
                hint="交接只传摘要与结构；原文请留在原角色，靠 context_digest 指认。",
            )


def _require_text(value: Any, where: str, *, max_len: int) -> str:
    if not isinstance(value, str):
        raise RoleError(ROLE_ERROR_HANDOFF, where + " 必须是字符串，实际 " + type(value).__name__)
    text = value.strip()
    if not text:
        raise RoleError(ROLE_ERROR_HANDOFF, where + " 不能为空")
    if len(text) > max_len:
        raise RoleError(
            ROLE_ERROR_HANDOFF,
            where + " 超过 " + str(max_len) + " 字上限（实际 " + str(len(text)) + "），"
            + "交接不允许搬运正文",
        )
    return text


def build_handoff(
    *,
    task: Any,
    context: Any,
    registry: Any,
    to_role: str,
    from_role: str = ROLE_SUPERVISOR,
    return_schema: Mapping[str, Any] | None = None,
    trace_id: str = "",
    created_at: str = "",
    extra: Mapping[str, Any] | None = None,
) -> tuple[Handoff, HandoffReceipt]:
    """构造交接包与回执。

    context 只用来算摘要，本身不进交接包——这是本模块最要紧的一条：
    接收方拿不到 context 原文，只能拿到 digest，想核对就回原角色问。
    """

    source = role_definition(from_role)
    target = role_definition(to_role)
    _assert_handoff_fields_safe(extra or {})
    task_text = _require_text(task, "task", max_len=HANDOFF_MAX_TASK_CHARS)
    digest = context_digest(context)
    if len(digest) > HANDOFF_MAX_DIGEST_CHARS:
        raise RoleError(ROLE_ERROR_HANDOFF, "context_digest 超过长度上限")
    tools_allowed = tuple(allowed_tool_names(registry, to_role))
    if len(tools_allowed) > HANDOFF_MAX_TOOLS:
        raise RoleError(
            ROLE_ERROR_HANDOFF,
            "允许的工具数量超过 " + str(HANDOFF_MAX_TOOLS) + " 项，交接包不再是摘要",
        )
    schema = dict(return_schema or {})
    if len(json.dumps(schema, ensure_ascii=False, default=str)) > HANDOFF_MAX_SCHEMA_CHARS:
        raise RoleError(
            ROLE_ERROR_HANDOFF,
            "return_schema 超过 " + str(HANDOFF_MAX_SCHEMA_CHARS) + " 字，交接包不再是结构说明",
        )
    handoff = Handoff(
        task=task_text,
        context_digest=digest,
        allowed_tools=tools_allowed,
        return_schema=schema,
        from_role=source.role,
        to_role=target.role,
    )
    receipt = HandoffReceipt(
        from_role=source.role,
        to_role=target.role,
        task=task_text,
        context_digest=digest,
        allowed_tools=tools_allowed,
        accepted=True,
        trace_id=str(trace_id or ""),
        created_at=str(created_at or ""),
    )
    return handoff, receipt


def reject_reason(role: str, tool_name: str, *, capability: str = "") -> str:
    """给拒绝写一句可核对的话：说清是哪个角色、哪个工具、哪类能力。"""

    definition = role_definition(role)
    caps = ", ".join(definition.capabilities) or "（无）"
    seen = str(capability or "未知能力")
    return (
        "角色 " + definition.role + " 不持有能力 " + seen + "，拒绝执行 " + str(tool_name)
        + "；该角色可用能力：" + caps
    )


def describe_roles() -> list[dict[str, Any]]:
    return [role_definition(item).to_dict() for item in ROLES]
