"""LocalRuntime：本机 Windows 子进程约束层。

再强调一次定位：这是**约束层**，不是安全隔离。它的价值在于把「能拦的先拦掉、
拦不掉的如实说清楚」，而不是假装自己是个容器。

执行流程（顺序不能乱）：
  1. 请求侧能力核对：请求要求的能力对不上矩阵就失败（除非显式接受降级）。
  2. 信任核对：非 trusted_fixture 且没有容器/gVisor 时直接拒执行。
  3. 源码大小核对：超限不启动进程。
  4. AST 静态拒绝：命中即拒绝，不启动进程。
  5. 建临时工作目录，写脚本文件。
  6. 建 JobObject，准备白名单环境，启动子进程并绑进 job。
  7. 超时/输出超限时终止整个 job，回收子进程树。
  8. 回收临时目录，返回带 isolation_level 与能力快照的结果。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pq_agent.sandbox.base import (
    SANDBOX_CAPABILITY_UNSUPPORTED,
    SANDBOX_NOT_EXECUTABLE,
    SANDBOX_OUTPUT_LIMIT,
    SANDBOX_POLICY_DENIED,
    SANDBOX_SOURCE_TOO_LARGE,
    SANDBOX_STATIC_BLOCKED,
    SANDBOX_TIMEOUT,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_POLICY_DENIED,
    STATUS_STATIC_BLOCKED,
    ExecRequest,
    ExecResult,
    SandboxError,
    blocked_result,
    check_requested_capabilities,
)
from pq_agent.sandbox.capabilities import ISOLATION_LEVEL, describe, require_capability
from pq_agent.sandbox.static_check import analyze

RUNTIME_NAME = "local"

# 子进程环境白名单：只有这些键会被继承下去
ENV_ALLOWLIST: tuple[str, ...] = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)

# 这些环境键必须显式注明「故意不传」，给测试与文档参照
ENV_DENYLIST: tuple[str, ...] = (
    "FEISHU_WEBHOOK_URL",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "LLM_API_KEY",
    "ANTHROPIC_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "USERPROFILE",
    "APPDATA",
    "HOME",
    "USERNAME",
)


def build_child_env(*, base: Mapping[str, str] | None = None, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """构造子进程环境：白名单 + 固定的 PATH + 强制 UTF-8 IO。

    PATH 单独处理：只保留系统目录与当前解释器目录，不带用户级路径。
    extra 只允许调用方补进「非敏感、可公开」的键；命中 DENYLIST 直接拒绝。
    """
    src = dict(base if base is not None else os.environ)
    out: dict[str, str] = {}
    for key in ENV_ALLOWLIST:
        if key in src:
            out[key] = src[key]
    # PATH：系统目录 + 解释器所在目录
    sysroot = src.get("SYSTEMROOT") or src.get("WINDIR") or ""
    parts: list[str] = []
    if sysroot:
        parts.append(str(Path(sysroot) / "System32"))
        parts.append(str(Path(sysroot)))
    parts.append(str(Path(sys.executable).parent))
    out["PATH"] = os.pathsep.join(parts)
    out["PYTHONIOENCODING"] = "utf-8"
    out["PYTHONDONTWRITEBYTECODE"] = "1"
    out["PYTHONNOUSERSITE"] = "1"
    if extra:
        for key, value in extra.items():
            if key in ENV_DENYLIST:
                raise SandboxError(SANDBOX_POLICY_DENIED, "不允许向子进程注入环境变量 %s" % key)
            out[key] = str(value)
    return out


@dataclass
class _JobHandle:
    """JobObject 包装。创建失败时 available=False，调用方必须如实上报。"""

    handle: int = 0
    available: bool = False
    detail: str = ""

    def assign(self, pid: int) -> bool:
        if not self.available:
            return False
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        handle = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, int(pid))
        if not handle:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(self.handle, handle))
        finally:
            kernel32.CloseHandle(handle)

    def terminate(self) -> bool:
        if not self.available:
            return False
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return bool(kernel32.TerminateJobObject(self.handle, 1))

    def close(self) -> None:
        if not self.available or not self.handle:
            return
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle(self.handle)
        self.handle = 0
        self.available = False


def create_job() -> _JobHandle:
    """尽力创建 JobObject；失败就返回 unavailable，不伪造资源限制。"""
    if sys.platform != "win32":
        return _JobHandle(available=False, detail="非 Windows 平台，不提供 job object")
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return _JobHandle(available=False, detail="CreateJobObjectW 返回空句柄")
        job = _JobHandle(handle=handle, available=True, detail="已创建")
        return job
    except Exception as exc:  # pragma: no cover - 平台相关
        return _JobHandle(available=False, detail="创建失败：%s" % type(exc).__name__)


def _kill_tree(pid: int, job: _JobHandle) -> bool:
    """结束进程树。优先用 job object（连带孙进程），退回 taskkill /T /F。"""
    killed = False
    if job.available and job.terminate():
        killed = True
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            killed = True
        except Exception:  # pragma: no cover - 平台相关
            pass
    return killed


@dataclass
class LocalRuntime:
    """本机子进程约束层。默认只用于演示与测试。"""

    root: Path | None = None
    work_parent: Path | None = None
    allow_untrusted: bool = False

    name: str = RUNTIME_NAME
    isolation_level: str = ISOLATION_LEVEL

    def capabilities(self) -> Mapping[str, Any]:
        return describe()

    def _work_parent(self) -> Path:
        if self.work_parent is not None:
            return Path(self.work_parent)
        base = Path(self.root) if self.root is not None else Path.cwd()
        return base / ".tmp" / "sandbox-runs"

    def execute(self, request: ExecRequest) -> ExecResult:
        """A4-02：外层包一个 sandbox.execute span，记录运行时、隔离级别与最终状态。

        为什么包在最外面而不是插进各条 return 分支：能力拒绝、策略拒绝、dry-run、
        超时、非零退出、成功一共六条出口，逐个打点迟早漏一条；包一层就只有一处。
        """
        from pq_agent.observability.apm_spans import (
            optional_tracer,
            sandbox_span,
            sandbox_status_holder,
        )

        tracer = optional_tracer()
        if tracer is None:
            return self._execute_inner(request)
        holder: dict[str, str] = {"status": "ok", "error_code": ""}
        try:
            cm = sandbox_span(
                tracer,
                runtime=self.name,
                isolation_level=self.isolation_level,
                status_holder=holder,
            )
            cm.__enter__()
        except Exception:
            # 观测层不许把主流程带崩：打点起不来就退回不带 span 的执行。
            return self._execute_inner(request)
        try:
            result = self._execute_inner(request)
        except BaseException:
            # 真出了异常：把 span 关掉再原样抛出，绝不吞。
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
            raise
        holder.update(sandbox_status_holder(result))
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass
        return result

    def _execute_inner(self, request: ExecRequest) -> ExecResult:
        # 1 能力核对
        try:
            check_requested_capabilities(request)
        except Exception as exc:
            code = getattr(exc, "code", SANDBOX_CAPABILITY_UNSUPPORTED)
            return blocked_result(
                request,
                status=STATUS_POLICY_DENIED,
                error_code=code,
                message=str(exc),
                runtime=self.name,
            )

        # 2 信任核对：没有真隔离就不跑不可信代码
        trusted = bool(request.trusted_fixture) or bool(self.allow_untrusted)
        if not trusted:
            return blocked_result(
                request,
                status=STATUS_POLICY_DENIED,
                error_code=SANDBOX_NOT_EXECUTABLE,
                message=("本机无容器与 gVisor，LocalRuntime 只允许执行已登记的良性 fixture；"
                         "请求未声明 trusted_fixture，已拒绝执行。"),
                runtime=self.name,
            )

        # 3 源码大小
        source_bytes = len(request.code.encode("utf-8"))
        if source_bytes > request.max_source_bytes:
            return blocked_result(
                request,
                status=STATUS_POLICY_DENIED,
                error_code=SANDBOX_SOURCE_TOO_LARGE,
                message="源码 %d 字节，超过上限 %d 字节" % (source_bytes, request.max_source_bytes),
                runtime=self.name,
            )
        stdin_bytes = len(request.stdin.encode("utf-8"))
        if stdin_bytes > request.max_source_bytes:
            return blocked_result(
                request,
                status=STATUS_POLICY_DENIED,
                error_code=SANDBOX_SOURCE_TOO_LARGE,
                message="stdin %d 字节，超过上限 %d 字节" % (stdin_bytes, request.max_source_bytes),
                runtime=self.name,
            )

        # 4 静态拒绝
        report = analyze(request.code)
        if report.blocked:
            if report.syntax_error:
                message = "源码无法解析：%s" % report.syntax_error
                findings: tuple[str, ...] = (("syntax", report.syntax_error),)
            else:
                message = "静态拒绝：命中 %d 条危险模式" % len(report.findings)
                findings = tuple(str(f) for f in report.findings)
            result = blocked_result(
                request,
                status=STATUS_STATIC_BLOCKED,
                error_code=SANDBOX_STATIC_BLOCKED,
                message=message,
                findings=findings,
                runtime=self.name,
            )
            return result

        # dry_run：到这一步说明「静态通过、策略允许」，但没有真的跑
        if request.dry_run:
            return ExecResult(
                status=STATUS_POLICY_DENIED,
                executed=False,
                error_code="SANDBOX_DRY_RUN",
                message="dry-run：静态与策略均通过，未真正执行",
                runtime=self.name,
                capabilities=describe(),
            )

        return self._run(request)

    def _run(self, request: ExecRequest) -> ExecResult:
        parent = self._work_parent()
        parent.mkdir(parents=True, exist_ok=True)
        work = parent / ("run-" + uuid.uuid4().hex[:12])
        work.mkdir(parents=True, exist_ok=True)
        script = work / "main.py"
        script.write_text(request.code, encoding="utf-8", newline=chr(10))

        env = build_child_env()
        timeout = request.effective_timeout()
        max_out = request.effective_max_output()
        job = create_job()
        started = time.monotonic()
        proc: subprocess.Popen[str] | None = None
        truncated = False
        error_code: str | None = None
        message = ""
        status = STATUS_OK

        flags = 0
        if sys.platform == "win32":
            flags = 0x00000200  # CREATE_NEW_PROCESS_GROUP

        try:
            # 必须传绝对路径：cwd 已经切到 work 目录，再给相对路径会被二次解析，
            # 变成 <work>/<work>/main.py 这种不存在的位置，子进程直接以退出码 2 结束
            # 却会被当成「执行过了但失败」，等于把「没跑起来」误报成「跑失败」。
            proc = subprocess.Popen(
                [sys.executable, "-I", "-B", str(script.resolve())],
                cwd=str(work),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=flags,
            )
            if job.available:
                job.assign(proc.pid)
            try:
                out, err = proc.communicate(input=request.stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_tree(proc.pid, job)
                try:
                    out, err = proc.communicate(timeout=5)
                except Exception:
                    out, err = "", ""
                status = STATUS_FAILED
                error_code = SANDBOX_TIMEOUT
                message = "执行超过 %ss 上限，已终止进程树" % timeout
        except Exception as exc:  # pragma: no cover - 兜底
            try:
                if proc is not None:
                    _kill_tree(proc.pid, job)
            except Exception:
                pass
            job.close()
            shutil.rmtree(work, ignore_errors=True)
            return ExecResult(
                status=STATUS_FAILED,
                executed=True,
                error_code="SANDBOX_INTERNAL",
                message="启动或等待子进程失败：%s" % type(exc).__name__,
                runtime=self.name,
                capabilities=describe(),
            )

        duration_ms = (time.monotonic() - started) * 1000.0
        out = out or ""
        err = err or ""

        # 输出超限：截断 + 标记，并如实说明这是「已发生的事实」
        if len(out.encode("utf-8")) > max_out:
            out = out.encode("utf-8")[:max_out].decode("utf-8", "replace")
            truncated = True
        if len(err.encode("utf-8")) > max_out:
            err = err.encode("utf-8")[:max_out].decode("utf-8", "replace")
            truncated = True

        exit_code = proc.returncode
        if status == STATUS_OK and exit_code != 0:
            status = STATUS_FAILED
            error_code = "SANDBOX_NONZERO_EXIT"
            message = "子进程退出码 %s" % exit_code
        if truncated and status == STATUS_OK:
            message = "输出超过 %d 字节上限，已截断" % max_out

        job.close()
        shutil.rmtree(work, ignore_errors=True)

        return ExecResult(
            status=status,
            executed=True,
            stdout=out,
            stderr=err,
            exit_code=exit_code,
            duration_ms=duration_ms,
            error_code=error_code,
            message=message,
            truncated=truncated,
            runtime=self.name,
            capabilities=describe(),
        )


def local_report() -> dict[str, Any]:
    """给 UI / 文档用的一份能力与局限快照。"""
    info = describe()
    info["env_allowlist"] = list(ENV_ALLOWLIST)
    info["env_denylist"] = list(ENV_DENYLIST)
    info["can_execute_untrusted"] = False
    return info
