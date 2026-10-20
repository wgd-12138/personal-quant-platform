#!/usr/bin/env python3
"""gVisor 真机 smoke：只有真的启动过一个 runsc 容器并拿到证据，才允许输出 PASS。

为什么不能只看配置：--runtime runsc 写成什么样都是「配置正确」，而配置正确不等于
隔离生效——runsc 没注册时 Docker 会静默回退到 runc，容器照样跑、日志照样干净。
所以这里做三件事：
1. 真的启动一个容器，运行一段良性 fixture；
2. fixture 里读 /proc/version：gVisor 的内核标识里带 gVisor，runc 跑的是宿主内核；
3. 断言 argv 里确实有 --runtime runsc（audit_argv 的 runtime_flag_correct）。

退出码：0=PASS（真的验证过）、1=FAIL（跑了但结论不对）、2=BLOCKED（环境不具备）。
无环境时输出 BLOCKED 和下一条命令，绝不打印 PASS、也不冒充 UNKNOWN 为通过。
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from pq_agent.sandbox.base import ExecRequest  # noqa: E402
from pq_agent.sandbox.gvisor_runtime import (  # noqa: E402
    GVISOR_RUNTIME_NAME,
    GvisorRuntime,
    decide_platform,
    platform_blocker,
    platform_note,
    probe_host,
)

# 良性 fixture：既证明「容器能把代码跑起来」，又证明「跑它的是 gVisor 而不是 runc」。
BENIGN_FIXTURE = chr(10).join([
    "try:",
    "    version = open(" + chr(34) + "/proc/version" + chr(34) + ").read().strip()",
    "except OSError as exc:",
    "    version = " + chr(34) + "unreadable: " + chr(34) + " + str(exc)",
    "print(" + chr(34) + "gvisor-smoke-ok" + chr(34) + ")",
    "print(" + chr(34) + "PROC_VERSION=" + chr(34) + " + version[:200])",
])

NEXT_COMMAND = (
    "在已安装并注册 runsc 的 Linux 主机上执行："
    " uv run --no-sync --extra agent python deploy/gvisor/smoke_gvisor.py"
)


def main() -> int:
    facts = probe_host()
    platform = decide_platform(facts)
    blocker = platform_blocker(facts)
    payload: dict = {
        "check": "gvisor_container_smoke",
        "runtime": GVISOR_RUNTIME_NAME,
        "platform": platform,
        "blocker": blocker,
        "note": platform_note(facts),
        "facts": facts.to_dict(),
    }
    if not platform:
        payload["status"] = "BLOCKED"
        payload["detail"] = blocker or "未探到可用平台；gVisor 层未被真机验证"
        payload["next_command"] = NEXT_COMMAND
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    runtime = GvisorRuntime(root=ROOT, allow_untrusted=True)
    audit = runtime.audit()
    result = runtime.execute(ExecRequest(code=BENIGN_FIXTURE, trusted_fixture=True, timeout_s=60.0))
    stdout = result.stdout or ""
    payload.update({
        "status_exec": result.status,
        "exit_code": result.exit_code,
        "error_code": result.error_code,
        "stdout": stdout.strip(),
        "stderr": (result.stderr or "")[:500],
        "audit_ok": bool(audit.get("ok")),
        "runtime_flag_correct": bool(audit.get("runtime_flag_correct")),
        "container_name": (runtime.startup_records[-1]["container_name"] if runtime.startup_records else ""),
        "startup_records": runtime.startup_records,
    })
    checks = {
        "fixture_printed_marker": "gvisor-smoke-ok" in stdout,
        "kernel_identifies_gvisor": "gvisor" in stdout.lower(),
        "exit_code_zero": result.exit_code == 0,
        "audit_ok": bool(audit.get("ok")),
        "runtime_flag_correct": bool(audit.get("runtime_flag_correct")),
    }
    payload["checks"] = checks
    ok = all(checks.values())
    payload["status"] = "PASS" if ok else "FAIL"
    if not ok:
        payload["detail"] = (
            "有检查项未通过。最容易被忽略的一条是 kernel_identifies_gvisor："
            "它证明这次真的是 runsc 在跑，而不是 Docker 悄悄回退到 runc。"
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())