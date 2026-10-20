"""沙箱执行合同：ExecRequest / ExecResult / SandboxRuntime。

三条不可动摇的规则：
1. 代码执行属于最高风险动作，必须逐次人工确认（由 graph 的审批层负责），
   本层只负责「确认之后怎么执行」以及「执行不了就明确拒绝」。
2. 上层提出的每条限制都要能对上能力矩阵；对不上就抛 SANDBOX_CAPABILITY_UNSUPPORTED，
   绝不静默降级成「我尽力」——那会让上层误以为限制已生效。
3. 任何结果都带 isolation_level 与 capabilities 快照，防止上层把约束层结果
   当成「安全隔离后的结果」去用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from pq_agent.sandbox.capabilities import (
    ISOLATION_LEVEL,
    UnsupportedCapabilityError,
    capability_map,
    describe,
)

# 沙箱错误码（统一前缀，便于在上层与遥测里识别）
SANDBOX_STATIC_BLOCKED = "SANDBOX_STATIC_BLOCKED"        # AST 静态拒绝：没执行
SANDBOX_POLICY_DENIED = "SANDBOX_POLICY_DENIED"          # 运行时策略拒绝：没执行
SANDBOX_CAPABILITY_UNSUPPORTED = "SANDBOX_CAPABILITY_UNSUPPORTED"
SANDBOX_SOURCE_TOO_LARGE = "SANDBOX_SOURCE_TOO_LARGE"
SANDBOX_TIMEOUT = "SANDBOX_TIMEOUT"
SANDBOX_OUTPUT_LIMIT = "SANDBOX_OUTPUT_LIMIT"
SANDBOX_NOT_EXECUTABLE = "SANDBOX_NOT_EXECUTABLE"
SANDBOX_INTERNAL = "SANDBOX_INTERNAL"

# ExecStatus：三条路径清清楚楚分开
STATUS_OK = "ok"                    # 真的执行了，拿到结果
STATUS_STATIC_BLOCKED = "static_blocked"    # 静态拒绝，没执行
STATUS_POLICY_DENIED = "policy_denied"      # 运行时策略拒绝，没执行
STATUS_FAILED = "failed"            # 执行了但失败（超时/输出超限/非零退出）

EXEC_STATUSES = (STATUS_OK, STATUS_STATIC_BLOCKED, STATUS_POLICY_DENIED, STATUS_FAILED)

# 默认上限（都可被 ExecRequest 覆盖，但只能收紧不能放宽）
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_OUTPUT_BYTES = 65536
DEFAULT_MAX_SOURCE_BYTES = 32768
DEFAULT_MAX_STDIN_BYTES = 32768

HARD_TIMEOUT_S = 60.0
HARD_MAX_OUTPUT_BYTES = 1048576
# 源码硬上限：effective_max_source() 只能在这个范围内收紧，不能放宽。
HARD_MAX_SOURCE_BYTES = 1048576


class SandboxError(RuntimeError):
    """沙箱层的统一异常。"""

    def __init__(self, code: str, detail: str = "") -> None:
        msg = code if not detail else "%s: %s" % (code, detail)
        super().__init__(msg)
        self.code = code


@dataclass(frozen=True)
class ExecRequest:
    """一次代码执行请求。

    code 是唯一要被执行的输入；其余字段全部是限制或受控数据，
    绝不允许调用方塞进「路径」「环境变量」「命令」这类逃逸口子。
    """

    code: str
    stdin: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES
    purpose: str = ""
    trusted_fixture: bool = False
    require: tuple[str, ...] = ()
    allow_unsupported: bool = False
    dry_run: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.code, str):
            raise SandboxError(SANDBOX_INTERNAL, "code 必须是字符串")
        if not isinstance(self.stdin, str):
            raise SandboxError(SANDBOX_INTERNAL, "stdin 必须是字符串")
        if self.timeout_s <= 0:
            raise SandboxError(SANDBOX_INTERNAL, "timeout_s 必须为正数")
        if self.max_output_bytes <= 0 or self.max_source_bytes <= 0:
            raise SandboxError(SANDBOX_INTERNAL, "大小上限必须为正数")

    def effective_timeout(self) -> float:
        return min(float(self.timeout_s), HARD_TIMEOUT_S)

    def effective_max_output(self) -> int:
        return min(int(self.max_output_bytes), HARD_MAX_OUTPUT_BYTES)

    def effective_max_source(self) -> int:
        return min(int(self.max_source_bytes), HARD_MAX_SOURCE_BYTES)

    def to_dict(self) -> dict[str, Any]:
        # 不把 code 原文塞进普通日志字段；只给长度与摘要字段
        return {
            "purpose": self.purpose,
            "code_bytes": len(self.code.encode("utf-8")),
            "stdin_bytes": len(self.stdin.encode("utf-8")),
            "timeout_s": self.effective_timeout(),
            "max_output_bytes": self.effective_max_output(),
            "trusted_fixture": self.trusted_fixture,
            "require": list(self.require),
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class ExecResult:
    """一次代码执行的结果。status 与 executed 必须自洽。"""

    status: str
    executed: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration_ms: float = 0.0
    error_code: str | None = None
    message: str = ""
    truncated: bool = False
    static_findings: tuple[str, ...] = ()
    isolation_level: str = ISOLATION_LEVEL
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    runtime: str = "local"

    def __post_init__(self) -> None:
        if self.status not in EXEC_STATUSES:
            raise SandboxError(SANDBOX_INTERNAL, "未知状态：%s" % self.status)
        # 自洽性：没执行就不可能拿到退出码；执行了失败就必须有退出码或错误码
        if not self.executed and self.exit_code is not None:
            raise SandboxError(SANDBOX_INTERNAL, "未执行的请求不允许带退出码")
        if self.executed and self.status == STATUS_FAILED and self.exit_code is None and not self.error_code:
            raise SandboxError(SANDBOX_INTERNAL, "执行失败必须带退出码或错误码")
        if self.status == STATUS_OK and not self.executed:
            raise SandboxError(SANDBOX_INTERNAL, "status=ok 必须真的执行了")
        # 阻断类状态不允许带 stdout：避免上层误读成「跑出来了一半」
        if self.status in (STATUS_STATIC_BLOCKED, STATUS_POLICY_DENIED) and (self.stdout or self.stderr):
            raise SandboxError(SANDBOX_INTERNAL, "被拒绝的请求不允许带输出")
        if self.status in (STATUS_STATIC_BLOCKED, STATUS_POLICY_DENIED) and not self.error_code:
            raise SandboxError(SANDBOX_INTERNAL, "被拒绝的请求必须带错误码")

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def blocked(self) -> bool:
        return self.status in (STATUS_STATIC_BLOCKED, STATUS_POLICY_DENIED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ok": self.ok,
            "executed": self.executed,
            "exit_code": self.exit_code,
            "duration_ms": round(self.duration_ms, 3),
            "error_code": self.error_code,
            "message": self.message,
            "truncated": self.truncated,
            "stdout_bytes": len(self.stdout.encode("utf-8")),
            "stderr_bytes": len(self.stderr.encode("utf-8")),
            "static_findings": list(self.static_findings),
            "isolation_level": self.isolation_level,
            "runtime": self.runtime,
        }


@runtime_checkable
class SandboxRuntime(Protocol):
    """沙箱运行时协议。LocalRuntime / DockerRuntime / GVisorRuntime 各自实现。"""

    name: str
    isolation_level: str

    def capabilities(self) -> Mapping[str, Any]:
        ...

    def execute(self, request: ExecRequest) -> ExecResult:
        ...


def check_requested_capabilities(request: ExecRequest, *, matrix: Mapping[str, Any] | None = None) -> None:
    """把请求里声明的限制逐条对上能力矩阵；对不上就显式失败。

    allow_unsupported 只用于「显式降级」这条唯一出口：调用方必须自己写下来
    它接受降级，并且我们在结果里照样回报 unsupported 列表，绝不当成已生效。
    """
    src = matrix or capability_map()
    missing: list[str] = []
    for key in request.require:
        item = src.get(key)
        if item is None:
            raise UnsupportedCapabilityError(key, "能力矩阵里没有登记这一项，无法承诺")
        supported = item.supported if hasattr(item, "supported") else bool(item.get("supported"))
        if not supported:
            missing.append(key)
    if missing and not request.allow_unsupported:
        detail = "；".join(missing)
        raise UnsupportedCapabilityError(detail, "调用方要求了这些限制，但本 runtime 无法强制，且未显式声明接受降级")


def blocked_result(
    request: ExecRequest,
    *,
    status: str,
    error_code: str,
    message: str,
    findings: Sequence[str] = (),
    runtime: str = "local",
    isolation_level: str | None = None,
    capabilities: Mapping[str, Any] | None = None,
) -> ExecResult:
    """构造一个「没执行」的结果，供静态拒绝与策略拒绝共用。

    isolation_level 必须由调用方按自身运行时传入，不能吃 ExecResult 的默认值：
    默认值是 LocalRuntime 的「constraint_only」，DockerRuntime 的拒绝结果套上它
    等于谎报隔离级别，审计脚本会据此得出「这条是约束层处理的」的错误结论。
    capabilities 同理：不传就会套上 LocalRuntime 的能力清单，Docker / gVisor 的
    拒绝结果会自称「只支持进程超时那几条」，把「为什么拒绝」这件事讲错。
    """
    return ExecResult(
        status=status,
        executed=False,
        error_code=error_code,
        message=message,
        static_findings=tuple(findings),
        runtime=runtime,
        isolation_level=isolation_level or ISOLATION_LEVEL,
        capabilities=capabilities if capabilities is not None else describe(),
    )
