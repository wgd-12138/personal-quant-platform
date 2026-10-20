"""角色化工具注册表：把 ToolRegistry 包一层，按角色收窄可见面并守住执行面。

为什么必须是包装而不是继承：
继承会让「有人拿基类引用绕过角色检查」成为可能，
而包装之后能拿到真表的只有本模块，其余代码只能通过 run_tool 进来。

本模块做的判定只有两条，且都不依赖提示词或模型自觉：
1. 可见面：schemas() 只返回该角色能力集合内的工具。
2. 执行面：invoke() 在真正执行前再查一次能力，越权抛 RoleError。
两条都要过。只做可见面等于「模型看不见就当它不存在」，
只做执行面等于把越权尝试藏起来，两者缺一都会让权限差异无法被测试证明。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pq_agent.roles.contracts import (
    ROLE_ERROR_CAPABILITY,
    ROLE_ERROR_UNKNOWN,
    ROLES,
    RoleError,
    allowed_tool_names,
    capabilities_for,
    reject_reason,
    role_definition,
    visible_schemas,
)

ROLE_REGISTRY_SCHEMA_VERSION = "pq_agent.role_registry/1"


class RoleRegistry:
    """一个角色视图。同一个真注册表可以派生出多个角色视图，互不影响。"""

    def __init__(self, registry: Any, role: str) -> None:
        if str(role) not in ROLES:
            raise RoleError(ROLE_ERROR_UNKNOWN, "未登记的角色：" + repr(role))
        # 包一个角色视图没有意义：那等于「守卫守着自己」，真注册表还在外面。
        # 必须包真注册表，且装配层（AgentService）只把包装后的对象交出去。
        if isinstance(registry, RoleRegistry):
            raise RoleError(
                ROLE_ERROR_UNKNOWN,
                "角色视图不能再次包装角色视图：" + repr(registry.role),
                hint="每个角色视图必须直接建在真注册表上，否则能力判定会叠加两层。",
            )
        self._registry = registry
        self._role = str(role)

    @property
    def role(self) -> str:
        return self._role

    @property
    def registry(self) -> Any:
        """真注册表。只读用途（例如体检时统计全量工具数）。"""
        return self._registry

    def capabilities(self) -> tuple[str, ...]:
        return capabilities_for(self._role)

    def describe(self) -> dict[str, Any]:
        return role_definition(self._role).to_dict()

    # ------------------------------------------------------------------ 可见面
    def names(self) -> list[str]:
        return allowed_tool_names(self._registry, self._role)

    def specs(self) -> list[Any]:
        allowed = set(self.names())
        return [spec for spec in self._registry.specs() if str(spec.name) in allowed]

    def schemas(self) -> list[dict[str, Any]]:
        return visible_schemas(self._registry, self._role)

    def visible_tool_names(self) -> list[str]:
        return self.names()

    # ------------------------------------------------------------------ 能力判定
    def may_use(self, tool_name: str) -> bool:
        return str(tool_name) in set(self.names())

    def capability_of(self, tool_name: str) -> str:
        return str(getattr(self._registry.get(tool_name), "capability", ""))

    # ------------------------------------------------------------------ 执行面
    def get(self, name: str) -> Any:
        """取工具规格。越权时抛 RoleError，而不是让调用方拿到规格自己判断。"""

        spec = self._registry.get(name)
        self._guard(name, capability=str(getattr(spec, "capability", "")))
        return spec

    def preview(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self.get(name)
        return self._registry.preview(name, arguments)

    def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        approval_token: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """执行前先过角色能力，再交给真注册表做审批与参数绑定校验。

        顺序不能反：先查审批再查角色，会让越权尝试带上一次合法审批的痕迹，
        事后翻 call_log 会看到一条「审批通过」的记录，看起来像是被允许的动作。
        """

        spec = self._registry.get(name)
        self._guard(name, capability=str(getattr(spec, "capability", "")))
        return self._registry.invoke(
            name,
            arguments,
            approval_token=approval_token,
            dry_run=dry_run,
        )

    def call_log(self) -> list[dict[str, Any]]:
        """真注册表的调用日志。越权调用**不会**进来，因为我们在它之前就拒绝了。"""

        return list(getattr(self._registry, "call_log", []) or [])

    def _guard(self, name: str, *, capability: str) -> None:
        if str(capability) in capabilities_for(self._role):
            return
        if _is_approval_only_control_write(self._registry, name, capability=capability):
            # control_write 是「写控制面（预览 / 回执）、必须人工确认」的能力，
            # 角色表里刻意没有分给任何角色（A5-01 的裁定）。它不对外发送、不写业务文件，
            # 唯一的作用是把动作排进审批：把整类拦掉，连「生成预览 → 等人批 → 执行」
            # 这条主流程都走不通，审批与断点恢复也就无从谈起。
            # 放行它不放宽任何东西：真注册表仍然要求 requires_approval 且 approval_token
            # 必须等于本次参数算出的 arguments_hash（见 ToolRegistry.invoke）。
            # business_write 与 notify_external 不走这条旁路，照常拒绝。
            return
        raise RoleError(
            ROLE_ERROR_CAPABILITY,
            reject_reason(self._role, name, capability=capability),
            hint="角色视图只暴露它自己的工具；需要更强的能力请换成 operator 并在审批后执行。",
        )

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return "RoleRegistry(role=" + self._role + ", tools=" + str(len(self.names())) + ")"


# 只靠人工确认把关的能力。它不属于任何角色，但每个角色视图都允许「排进审批」。
APPROVAL_ONLY_CAPABILITIES: tuple[str, ...] = ("control_write",)


def _is_approval_only_control_write(registry: Any, name: str, *, capability: str) -> bool:
    """这个越权工具是不是「只靠人工确认把关」的那一类。

    判定必须来自工具规格本身（能力 + requires_approval），不另外维护一张表：
    另抄一份权限表，迟早会和 ToolSpec 对不上。取不到规格就不放行（fail-closed）。
    """
    if str(capability) not in APPROVAL_ONLY_CAPABILITIES:
        return False
    spec = getattr(registry, "_specs", {}).get(str(name)) if hasattr(registry, "_specs") else None
    if spec is None:
        getter = getattr(registry, "get", None)
        if callable(getter):
            try:
                spec = getter(str(name))
            except Exception:  # noqa: BLE001 - 取不到规格就不放行
                return False
    return bool(getattr(spec, "requires_approval", False))


def role_registry(registry: Any, role: str) -> RoleRegistry:
    return RoleRegistry(registry, role)


def all_role_views(registry: Any) -> dict[str, RoleRegistry]:
    return {role: RoleRegistry(registry, role) for role in ROLES}


__all__ = ["ROLE_REGISTRY_SCHEMA_VERSION", "RoleRegistry", "role_registry", "all_role_views"]
