"""code_exec 工具：把「模型生成的代码」变成一次受控的沙箱调用。

设计要点（对应 A6-03 三.1 / 三.2）：
1. code_exec 是合法能力，但在合同里独立成类：它不是只读，也不是写盘或外发，
   而是「在沙箱里执行代码」。ToolSpec / ToolCall / Approval / JSON schema
   四处枚举必须同名同义，否则会出现「schema 里看不到、执行却没被拦住」。
2. 永远要人工确认：capability=code_exec 落在 APPROVAL_REQUIRED_CAPABILITIES，
   且 side_effects 非空（ToolSpec 合同强制 side_effects -> requires_approval）。
3. 运行时由部署方在装配时注入，模型无法选择。本工具只接受声明了
   untrusted_code_safe 的运行时（Docker / gVisor）。LocalRuntime 明确声明这条
   能力是 unsupported，所以本机（没有 Docker）执行模型生成的代码会被拒绝，
   绝不回落成本机子进程 —— 那只是约束层，不是隔离。
4. dry_run 或 mode=preview 只回预览（源码指纹、限制、运行时身份），不执行一行代码。
5. 源码与用户 stdin 不进日志字段：预览里只给长度与 sha256。
"""

from __future__ import annotations

import hashlib
from typing import Any

from pq_agent.sandbox.base import (
    DEFAULT_TIMEOUT_S,
    ExecRequest,
    SandboxError,
    STATUS_OK,
)
from pq_agent.tools.contracts import ArgSpec, ToolError, ToolSpec, build_result

CODE_EXEC_TOOL_NAME = "code_exec"
CODE_EXEC_MODES = ("preview", "execute")
CODE_EXEC_MODE_DEFAULT = "preview"
# 只接受声明了这条能力的运行时：它是「可以执行不可信代码」的唯一凭据。
CODE_EXEC_REQUIRED_CAPABILITIES = ("untrusted_code_safe",)
CODE_EXEC_MAX_TIMEOUT_S = 60
CODE_EXEC_MIN_TIMEOUT_S = 1
CODE_EXEC_PURPOSE = "agent_code_exec"
CODE_EXEC_SIDE_EFFECTS = ("code_execution",)
# stderr 只回最后这么多字符：够定位错误，又不至于把整段输出塞进 state。
CODE_EXEC_STDERR_TAIL = 800


def _source_digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def make_code_exec_spec(
    *,
    sandbox: Any,
    max_timeout_s: int = CODE_EXEC_MAX_TIMEOUT_S,
    purpose_note: str = "",
) -> ToolSpec:
    """构造 code_exec 工具合同。sandbox 必须是部署方选定的运行时，不能为空。"""

    if sandbox is None:
        raise ToolError("TOOL_SCHEMA_INVALID", "code_exec 必须绑定一个沙箱运行时，不能为空")

    runtime_name = str(getattr(sandbox, "name", "") or "unknown")
    isolation_level = str(getattr(sandbox, "isolation_level", "") or "unknown")

    def handler(arguments: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        source = str(arguments.get("source") or "")
        if not source.strip():
            raise ToolError("TOOL_SCHEMA_INVALID", "code_exec 的 source 不能为空")
        stdin = str(arguments.get("stdin") or "")
        mode = str(arguments.get("mode") or CODE_EXEC_MODE_DEFAULT)
        timeout_s = int(arguments.get("timeout_s") or int(DEFAULT_TIMEOUT_S))
        request = ExecRequest(
            code=source,
            stdin=stdin,
            timeout_s=float(timeout_s),
            purpose=CODE_EXEC_PURPOSE,
            require=CODE_EXEC_REQUIRED_CAPABILITIES,
        )
        # 预览路径：dry_run 或 mode=preview 都不执行。两个条件都要看，
        # 因为「计划声明不要副作用」和「调用方只想看看」是两件不同的事。
        if dry_run or mode != "execute":
            return build_result(
                spec,
                data={
                    "mode": "preview",
                    "executed": False,
                    "source_bytes": len(source.encode("utf-8")),
                    "source_sha256": _source_digest(source),
                    "stdin_bytes": len(stdin.encode("utf-8")),
                    "timeout_s": request.effective_timeout(),
                    "max_output_bytes": request.effective_max_output(),
                    "runtime": runtime_name,
                    "isolation_level": isolation_level,
                    "required_capabilities": list(CODE_EXEC_REQUIRED_CAPABILITIES),
                    "note": "预览：未执行任何代码。" + (purpose_note or ""),
                },
                summary="已生成代码执行预览（未执行）",
                data_source="sandbox_preview",
                dry_run=True,
            )
        try:
            result = sandbox.execute(request)
        except SandboxError as exc:
            raise ToolError(
                "TOOL_SANDBOX_BLOCKED",
                str(exc),
                hint="沙箱拒绝了这次执行，请按错误码处理，不要在本机重试。",
            ) from exc
        except RuntimeError as exc:
            code = str(getattr(exc, "code", "") or "")
            if code == "SANDBOX_CAPABILITY_UNSUPPORTED":
                raise ToolError(
                    "TOOL_SANDBOX_BLOCKED",
                    str(exc),
                    hint="该运行时没有 untrusted_code_safe 能力，不能执行模型生成的代码。",
                ) from exc
            raise
        if result.blocked:
            raise ToolError(
                "TOOL_SANDBOX_BLOCKED",
                "%s：%s" % (result.error_code, result.message or "沙箱拒绝执行"),
                details={"status": result.status, "error_code": str(result.error_code), "runtime": result.runtime},
            )
        # 执行了但退出码非零 / 超时 / 输出超限：工具调用本身是成功的（我们拿到了可信观测），
        # 失败属于用户代码。所以 ok=True，但必须把 status、退出码与 stderr 尾巴原样交出去，
        # 并用 degraded + warnings 让上层看得见，不能伪装成一次干净的成功。
        warnings: list[str] = []
        if result.status != STATUS_OK:
            warnings.append("沙箱执行未成功：status=%s error_code=%s" % (result.status, result.error_code))
        if result.exit_code not in (0, None):
            warnings.append("代码退出码非零：%s" % result.exit_code)
        if result.truncated:
            warnings.append("输出超过上限，已截断")
        degraded: tuple[str, ...] = ()
        if result.status != STATUS_OK:
            degraded = ("sandbox_execution_failed",)
        if result.truncated:
            degraded = degraded + ("output_truncated",)
        return build_result(
            spec,
            data={
                "mode": "execute",
                "status": result.status,
                "executed": bool(result.executed),
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr_tail": result.stderr[-CODE_EXEC_STDERR_TAIL:],
                "truncated": bool(result.truncated),
                "duration_ms": round(result.duration_ms, 3),
                "runtime": result.runtime,
                "isolation_level": result.isolation_level,
            },
            summary="沙箱执行完成：status=%s exit_code=%s" % (result.status, result.exit_code),
            data_source="sandbox:" + str(result.runtime),
            warnings=warnings,
            degraded=degraded,
            dry_run=False,
        )

    spec = ToolSpec(
        name=CODE_EXEC_TOOL_NAME,
        capability="code_exec",
        side_effects=CODE_EXEC_SIDE_EFFECTS,
        args=(
            ArgSpec("source", "string", required=True, description="要执行的 Python 源码（在沙箱内运行）"),
            ArgSpec("stdin", "string", description="传给用户代码的标准输入"),
            ArgSpec("mode", "enum", choices=CODE_EXEC_MODES, description="preview 只预览；execute 才真的执行"),
            ArgSpec("timeout_s", "integer", minimum=CODE_EXEC_MIN_TIMEOUT_S, maximum=max_timeout_s, description="超时秒数"),
        ),
        returns={"type": "object"},
        timeout_s=float(max_timeout_s),
        idempotency="requires_approval",
        risk="high",
        summary="在沙箱里执行模型生成的 Python 代码（必须人工确认，且只接受有 untrusted_code_safe 能力的运行时）",
        cli_command="",
        requires_approval=True,
        handler=handler,
    )
    return spec
