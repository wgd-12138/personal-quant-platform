"""多角色隔离层：supervisor / analyst / operator 的最小可解释分工。

这个包只做一件事：把「谁能看到哪些工具、谁能执行哪些能力」写成代码，
并用测试证明越权会被拒绝。它不负责审批、不负责路由执行、不负责并发调度。
角色拿到的能力集合只影响「可见面」（schema），执行面由 graph 的审批与
registry 的 arguments_hash 校验继续把关，两层同时成立才算越权被拦住。
"""

from __future__ import annotations

from pq_agent.roles import analyst, operator, supervisor
from pq_agent.roles.role_registry import RoleRegistry, all_role_views  # noqa: E402
from pq_agent.roles.role_registry import role_registry as make_role_registry  # noqa: E402
from pq_agent.roles.contracts import (
    HANDOFF_FORBIDDEN_FIELDS,
    HANDOFF_MAX_DIGEST_CHARS,
    HANDOFF_MAX_SCHEMA_CHARS,
    HANDOFF_MAX_TASK_CHARS,
    HANDOFF_MAX_TOOLS,
    HANDOFF_SCHEMA_VERSION,
    ROLE_ANALYST,
    ROLE_CAPABILITIES,
    ROLE_CAPABILITY_DENIED_CODE,
    ROLE_ERROR_CAPABILITY,
    ROLE_ERROR_FIELD,
    ROLE_ERROR_HANDOFF,
    ROLE_ERROR_UNKNOWN,
    ROLE_OPERATOR,
    ROLE_PURPOSE,
    ROLE_SCHEMA_VERSION,
    ROLE_SUPERVISOR,
    ROLES,
    Handoff,
    HandoffReceipt,
    RoleDefinition,
    RoleError,
    allowed_tool_names,
    build_handoff,
    capabilities_for,
    context_digest,
    describe_roles,
    may_use_capability,
    reject_reason,
    role_definition,
    visible_schemas,
)

__all__ = [
    "analyst",
    "operator",
    "supervisor",
    "HANDOFF_FORBIDDEN_FIELDS",
    "HANDOFF_MAX_DIGEST_CHARS",
    "HANDOFF_MAX_SCHEMA_CHARS",
    "HANDOFF_MAX_TASK_CHARS",
    "HANDOFF_MAX_TOOLS",
    "HANDOFF_SCHEMA_VERSION",
    "Handoff",
    "HandoffReceipt",
    "ROLE_ANALYST",
    "ROLE_CAPABILITIES",
    "ROLE_CAPABILITY_DENIED_CODE",
    "ROLE_ERROR_CAPABILITY",
    "ROLE_ERROR_FIELD",
    "ROLE_ERROR_HANDOFF",
    "ROLE_ERROR_UNKNOWN",
    "ROLE_OPERATOR",
    "ROLE_PURPOSE",
    "ROLE_SCHEMA_VERSION",
    "ROLE_SUPERVISOR",
    "ROLES",
    "RoleDefinition",
    "RoleError",
    "allowed_tool_names",
    "build_handoff",
    "capabilities_for",
    "context_digest",
    "describe_roles",
    "may_use_capability",
    "reject_reason",
    "role_definition",
    "visible_schemas",
    "RoleRegistry",
    "all_role_views",
    "make_role_registry",
]
