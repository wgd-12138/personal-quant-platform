#!/usr/bin/env python3
"""gVisor 层验收：每一步只允许四种结论——PASS / FAIL / BLOCKED / UNKNOWN。

为什么重写成 Python：上一版 shell 脚本有两处把「没测」说成了「没问题」。
  1. 缺 ss 时 `ss -ltnp | grep ... && fail || pass` 会因为命令本身失败直接落到 pass，
     打印「没有发现公网监听」——它其实一次都没检查过。shell 里这类静默落点是结构性的，
     而 Python 里 shutil.which() 能把「命令在不在」当成一个可判定的结论。
  2. argv 审计通过就打「全绿」，但配置全绿不等于 gVisor 可用：runsc 没注册时
     Docker 会静默回退到 runc，容器照跑、配置照绿。所以可用性必须由真机 smoke 判定。

退出码：0=全部 PASS；1=有 FAIL；2=没有 FAIL 但有 BLOCKED/UNKNOWN（环境不完整）。
本脚本只做只读检查，唯一会启动容器的一步是 smoke_gvisor.py。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
UNKNOWN = "UNKNOWN"

# 只检查这几个端口有没有绑到通配地址；绑在 127.0.0.1 上是设计如此。
WILDCARD_PORT_RE = re.compile(r"(0\.0\.0\.0|\[::\]):(8123|4317|4318|11800)\b")


class Ledger:
    """记分板：把每一步的结论原样留下来，最后一起输出。"""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(self, status: str, name: str, detail: str) -> None:
        self.rows.append({"status": status, "name": name, "detail": detail})
        print("  [%-7s] %s — %s" % (status, name, detail))

    @property
    def failed(self) -> bool:
        return any(row["status"] == FAIL for row in self.rows)

    @property
    def incomplete(self) -> bool:
        return any(row["status"] in (BLOCKED, UNKNOWN) for row in self.rows)

    def exit_code(self) -> int:
        if self.failed:
            return 1
        if self.incomplete:
            return 2
        return 0


def section(title: str) -> None:
    print("")
    print("== " + title + " ==")


def check_platform(ledger: Ledger) -> str:
    section("1. 平台判定")
    try:
        from pq_agent.sandbox.gvisor_runtime import (
            decide_platform,
            platform_blocker,
            platform_note,
            probe_host,
        )
    except Exception as exc:  # noqa: BLE001
        ledger.record(FAIL, "platform_probe", "导入 gvisor_runtime 失败：" + type(exc).__name__ + ": " + str(exc))
        return ""
    facts = probe_host()
    platform = decide_platform(facts)
    blocker = platform_blocker(facts)
    print(json.dumps({"platform": platform, "blocker": blocker, "note": platform_note(facts), "facts": facts.to_dict()}, ensure_ascii=False, indent=2))
    if platform:
        ledger.record(PASS, "platform_probe", "平台=" + platform)
    else:
        ledger.record(BLOCKED, "platform_probe", "不可用原因：" + (blocker or "未探到可用平台"))
    return platform


def check_docker(ledger: Ledger) -> None:
    section("2. Docker 与 runsc 注册")
    docker = shutil.which("docker")
    if docker is None:
        ledger.record(BLOCKED, "docker_present", "没有 docker 命令，容器侧一律未验证")
        return
    ledger.record(PASS, "docker_present", docker)
    try:
        completed = subprocess.run(
            [docker, "info", "--format", "{{json .Runtimes}}"],
            capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        ledger.record(BLOCKED, "runsc_registered", "docker info 执行失败：" + type(exc).__name__)
        return
    runtimes = (completed.stdout or "").strip()
    if completed.returncode != 0 or not runtimes:
        ledger.record(BLOCKED, "runsc_registered", "拿不到 Runtimes（守护进程未运行或权限不足）")
    elif "runsc" in runtimes:
        ledger.record(PASS, "runsc_registered", runtimes)
    else:
        ledger.record(FAIL, "runsc_registered", "runsc 未注册：" + runtimes)


def check_config_audit(ledger: Ledger) -> None:
    section("3. 加固参数与 argv 审计（静态配置，不代表可用）")
    try:
        from pq_agent.sandbox.gvisor_runtime import gvisor_report

        report = gvisor_report(root=ROOT)
        audit = report["audit"]
        shown = {key: value for key, value in audit.items() if key != "argv"}
        print(json.dumps(shown, ensure_ascii=False, indent=2))
        if audit.get("ok"):
            ledger.record(PASS, "argv_config_audit", "加固参数与 --runtime runsc 配置正确（这只是配置，不是可用性证据）")
        else:
            ledger.record(FAIL, "argv_config_audit", "argv 审计未通过：" + json.dumps(shown, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        ledger.record(FAIL, "argv_config_audit", type(exc).__name__ + ": " + str(exc))


def check_redteam_static(ledger: Ledger) -> int:
    section("4. 红队语料（静态层）")
    corpus = ROOT / "agent_eval" / "cases" / "sandbox_redteam.json"
    if not corpus.exists():
        ledger.record(FAIL, "redteam_static_layer", "语料不存在：" + str(corpus))
        return 0
    try:
        from pq_agent.sandbox import static_check

        data = json.loads(corpus.read_text(encoding="utf-8"))
        cases = data.get("cases") or data.get("items") or []
        counts = data.get("counts") or {}
        malicious = [case for case in cases if case.get("kind") == "malicious"]
        benign = [case for case in cases if case.get("kind") == "benign"]
        # 按语料自己声明的层级分组：资源耗尽类（while True / 起线程）本来就不该被静态层拦下，
        # 硬要求「恶意 10/10 全被静态拦」会让验收永远红灯，而放宽成「拦住几条就算过」又等于取消判定。
        static_cases = [case for case in malicious if case.get("block_layer") == "static"]
        runtime_cases = [case for case in malicious if case.get("block_layer") != "static"]
        blocked = sum(1 for case in static_cases if static_check.analyze(case["code"]).blocked)
        # 反向检查：声明归运行时层的样本，静态层不许误报成已拦，否则「执行层兜住了」只是句空话
        leaked = [case["case_id"] for case in runtime_cases if static_check.analyze(case["code"]).blocked]
        clear = sum(1 for case in benign if not static_check.analyze(case["code"]).blocked)
        declared_static = int(counts.get("static_expected") or len(static_cases))
    except Exception as exc:  # noqa: BLE001
        ledger.record(FAIL, "redteam_static_layer", type(exc).__name__ + ": " + str(exc))
        return 0
    print(json.dumps({"malicious": len(malicious), "static_declared": declared_static, "static_blocked": blocked, "runtime_declared": len(runtime_cases), "benign": len(benign), "benign_clear": clear, "runtime_leaked_to_static": leaked}, ensure_ascii=False))
    detail = "按语料声明：静态层拦下 %d/%d 条静态样本，良性 %d/%d 通过；另 %d 条资源耗尽类归运行时层" % (blocked, len(static_cases), clear, len(benign), len(runtime_cases))
    if static_cases and len(static_cases) == declared_static and blocked == len(static_cases) and clear == len(benign) and not leaked:
        ledger.record(PASS, "redteam_static_layer", detail)
    else:
        ledger.record(FAIL, "redteam_static_layer", detail + "；静态层误拦的运行时样本：" + (", ".join(leaked) if leaked else "无"))
    return len(runtime_cases)


def check_smoke(ledger: Ledger) -> bool:
    section("5. 真机 smoke（真的启动一个 runsc 容器）")
    script = ROOT / "deploy" / "gvisor" / "smoke_gvisor.py"
    if not script.exists():
        ledger.record(FAIL, "gvisor_container_smoke", "缺少 smoke 脚本：" + str(script))
        return False
    env = dict(**{k: v for k, v in __import__("os").environ.items()}, PYTHONPATH="src")
    completed = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=300,
        cwd=str(ROOT), env=env, encoding="utf-8", errors="replace",
    )
    print(completed.stdout or "")
    if completed.stderr and completed.stderr.strip():
        print("[stderr] " + completed.stderr.strip()[:800])
    if completed.returncode == 0:
        ledger.record(PASS, "gvisor_container_smoke", "容器真的跑起来了，且 /proc/version 证明是 gVisor")
        return True
    if completed.returncode == 1:
        ledger.record(FAIL, "gvisor_container_smoke", "容器跑了但结论不对（见上面 checks）")
        return False
    ledger.record(BLOCKED, "gvisor_container_smoke", "环境不具备，gVisor 隔离未被真机验证")
    return False


def check_redteam_runtime(ledger: Ledger, smoke_passed: bool, runtime_cases: int) -> None:
    section("6. 红队语料（运行时层）")
    detail = "语料声明 %d 条资源耗尽类（超时 / 进程数）" % runtime_cases
    if smoke_passed:
        ledger.record(PASS, "redteam_runtime_layer", detail + "，已在真机容器上验证")
    else:
        ledger.record(UNKNOWN, "redteam_runtime_layer", detail + "，必须在真机容器上量，本机未验证（不是通过）")


def check_exposure(ledger: Ledger) -> None:
    section("7. 暴露面")
    ss = shutil.which("ss")
    if ss is None:
        ledger.record(BLOCKED, "exposure_scan", "没有 ss 命令，暴露面一次都没检查（旧版脚本会在这里误报 PASS）")
        return
    try:
        completed = subprocess.run(
            [ss, "-ltn"], capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except Exception as exc:  # noqa: BLE001
        ledger.record(BLOCKED, "exposure_scan", "ss 执行失败：" + type(exc).__name__)
        return
    listing = (completed.stdout or "").strip()
    if not listing:
        ledger.record(BLOCKED, "exposure_scan", "ss 没返回任何监听信息，无法判定")
        return
    hits = [line for line in listing.splitlines() if WILDCARD_PORT_RE.search(line)]
    if hits:
        ledger.record(FAIL, "exposure_scan", "发现端口绑到公网地址：" + "；".join(hits[:3]))
    else:
        ledger.record(PASS, "exposure_scan", "8123/4317/4318/11800 未绑到公网地址")


def main() -> int:
    ledger = Ledger()
    print("== gVisor 层验收（PASS/FAIL/BLOCKED/UNKNOWN） ==")
    check_platform(ledger)
    check_docker(ledger)
    check_config_audit(ledger)
    runtime_cases = check_redteam_static(ledger)
    smoke_passed = check_smoke(ledger)
    check_redteam_runtime(ledger, smoke_passed, runtime_cases)
    check_exposure(ledger)
    section("汇总")
    print(json.dumps({"results": ledger.rows, "failed": ledger.failed, "incomplete": ledger.incomplete}, ensure_ascii=False, indent=2))
    code = ledger.exit_code()
    if code == 1:
        print("验收未通过：有 FAIL 项，请逐条处理。")
    elif code == 2:
        print("验收未完成：有 BLOCKED/UNKNOWN 项。gVisor 层只能算「配置已审计、真机未验证」，不能算通过。")
    else:
        print("验收通过：全部检查项都真的跑过并通过。")
    return code


if __name__ == "__main__":
    sys.exit(main())