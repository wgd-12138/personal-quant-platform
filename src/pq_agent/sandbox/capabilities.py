"""沙箱能力矩阵：把「支持 / 已强制 / 不支持」三态显式写出来。

为什么要三态而不是布尔：Windows 上很多限制做不到（内存配额、只读挂载、
真正的网络隔离），把它们写成 True 就变成了一句谎话。企业 agent 的沙箱
必须能对上层说清楚「这条限制我做不到」，否则上层会把「我以为被限制住了」
当成「已经限制住了」。

本文件只描述 LocalRuntime（本机 Windows 子进程）。Docker / gVisor 的实现
在 A2-02、A2-03 接入，能力矩阵各自独一份，绝不共用一份「都支持」的表。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

# 支持状态三态
SUPPORTED = "supported"        # 已实现，并且有测试证明它真的起作用
UNSUPPORTED = "unsupported"    # 本 runtime 做不到，上层必须自己兜底

# LocalRuntime 的隔离级别：只是约束层，不是安全隔离
ISOLATION_LEVEL = "constraint_only"
ISOLATION_LEVEL_NOTE = (
    "约束层（constraint layer）：本机子进程 + 显式白名单环境 + 静态拒绝 + 超时 + 输出上限。"
    "它不是安全隔离，恶意代码仍可能读写当前用户可访问的文件、访问网络、读取宿主信息。"
    "真正隔离需要容器或 gVisor（A2-02 / A2-03）。"
)


@dataclass(frozen=True)
class Capability:
    """一条能力声明。"""

    key: str
    status: str
    detail: str
    enforced_by: str = ""

    def __post_init__(self) -> None:
        if self.status not in (SUPPORTED, UNSUPPORTED):
            raise ValueError("未知能力状态：%s" % self.status)

    @property
    def supported(self) -> bool:
        return self.status == SUPPORTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "supported": self.supported,
            "detail": self.detail,
            "enforced_by": self.enforced_by,
        }


class UnsupportedCapabilityError(RuntimeError):
    """上层要求了一条本 runtime 做不到、且不允许静默降级的限制。"""

    def __init__(self, key: str, detail: str = "") -> None:
        msg = "SANDBOX_CAPABILITY_UNSUPPORTED: %s" % key
        if detail:
            msg = msg + "：" + detail
        super().__init__(msg)
        self.code = "SANDBOX_CAPABILITY_UNSUPPORTED"
        self.key = key


# LocalRuntime 的能力矩阵（全部实测过，不是照抄文档）
LOCAL_CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "process_timeout",
        SUPPORTED,
        "子进程超时后 kill；实测 2.02s 触发 TimeoutExpired，随后 tasklist 无残留 python 进程。",
        "subprocess.run(timeout=...) + 超时后任务树终止",
    ),
    Capability(
        "stdout_stderr_limit",
        SUPPORTED,
        "输出超过上限即截断并在结果里标记 truncated，同时结束进程避免继续产出。",
        "读取时按字节上限截断",
    ),
    Capability(
        "source_size_limit",
        SUPPORTED,
        "执行前先量源码字节数，超限直接拒绝，不启动进程。",
        "执行前静态检查",
    ),
    Capability(
        "environment_allowlist",
        SUPPORTED,
        "子进程环境只保留 PATH / SYSTEMROOT / PYTHONIOENCODING 等固定键，其余一律清空。",
        "subprocess 的 env 参数传全新字典",
    ),
    Capability(
        "cwd_confined",
        SUPPORTED,
        "工作目录固定为每次执行新建的临时子目录，项目真实目录不出现在 cwd 里。",
        "subprocess 的 cwd 参数",
    ),
    Capability(
        "static_denylist",
        SUPPORTED,
        "AST 解析后拒绝危险导入、动态执行、对象逃逸模式。这只是快速拒绝层，拦不住绕过写法。",
        "ast 遍历 denylist",
    ),
    Capability(
        "process_tree_kill",
        SUPPORTED,
        "创建 JobObject 把子进程树绑进去，超时用 TerminateJobObject 连带孙进程一起结束。",
        "CreateJobObjectW + AssignProcessToJobObject + TerminateJobObject",
    ),
    Capability(
        "network_isolation",
        UNSUPPORTED,
        "本机子进程无法真正断网：实测 socket.connect_ex 返回 10035（阻塞超时），说明连接尝试真的发出去了。",
        "",
    ),
    Capability(
        "filesystem_readonly",
        UNSUPPORTED,
        "无法把子进程的写权限限制在某个目录；子进程继承当前用户的全部文件权限。",
        "",
    ),
    Capability(
        "memory_limit",
        UNSUPPORTED,
        "内存配额需要在 JobObject 上设 LimitFlags 并可信验证生效；本机未做可信验证，故保守标为不支持。",
        "",
    ),
    Capability(
        "cpu_limit",
        UNSUPPORTED,
        "只能设墙钟超时；无法限制 CPU 时间片或核数。",
        "",
    ),
    Capability(
        "untrusted_code_safe",
        UNSUPPORTED,
        "因为网络与文件权限无法隔离，LocalRuntime 只允许执行已知良性 fixture；不可信或涉敏代码必须拒执行。",
        "",
    ),
)


def capability_map() -> dict[str, Capability]:
    return {item.key: item for item in LOCAL_CAPABILITIES}


def is_supported(key: str) -> bool:
    item = capability_map().get(key)
    return bool(item and item.supported)


def require_capability(key: str) -> None:
    """上层要求某条限制时必须先喊一声；做不到就显式失败，绝不静默降级。"""
    item = capability_map().get(key)
    if item is None:
        raise UnsupportedCapabilityError(key, "能力矩阵里没有登记这一项")
    if not item.supported:
        raise UnsupportedCapabilityError(key, item.detail)


def describe() -> dict[str, Any]:
    return {
        "runtime": "local",
        "isolation_level": ISOLATION_LEVEL,
        "note": ISOLATION_LEVEL_NOTE,
        "supported": [c.key for c in LOCAL_CAPABILITIES if c.supported],
        "unsupported": [c.key for c in LOCAL_CAPABILITIES if not c.supported],
        "capabilities": [c.to_dict() for c in LOCAL_CAPABILITIES],
    }


# 沙箱层自己挑 runtime 的开关。必须由部署方显式打开，且只在真实能力已被
# 探针证实后才有意义；没打开时一律走 LocalRuntime，绝不默认升级也绝不默认降级。
ENV_SANDBOX_RUNTIME = "PQ_AGENT_SANDBOX_RUNTIME"
RUNTIME_LOCAL = "local"
RUNTIME_DOCKER = "docker"
RUNTIME_GVISOR = "gvisor"


def select_runtime_key(*, environ: Mapping[str, str] | None = None) -> str:
    """返回本次进程该用的沙箱运行时键。

    只认 "local"、"docker" 和 "gvisor" 三个值，其余一律按 "local" 处理并保持沉默：
    这里的选择只决定「用哪份实现」，不决定「能不能执行」——能不能执行由各
    runtime 自己的能力矩阵回答，DockerRuntime 不可用时仍然拒绝执行。
    """
    src = environ if environ is not None else os.environ
    raw = str(src.get(ENV_SANDBOX_RUNTIME, "") or "").strip().lower()
    if raw == RUNTIME_DOCKER:
        return RUNTIME_DOCKER
    if raw == RUNTIME_GVISOR:
        return RUNTIME_GVISOR
    return RUNTIME_LOCAL


def unsupported_keys(matrix: Mapping[str, Any] | None = None) -> list[str]:
    src = matrix or capability_map()
    out: list[str] = []
    for key, item in src.items():
        if isinstance(item, Capability):
            if not item.supported:
                out.append(key)
        elif isinstance(item, Mapping) and not item.get("supported"):
            out.append(key)
    return sorted(out)
