"""GVisorRuntime：gVisor（runsc）用户态内核隔离层。

三层沙箱的第三层，和 DockerRuntime 的关系是「继承配置、换掉内核边界」：
1. 加固参数、挂载白名单、审计逻辑全部复用 DockerRuntime，绝不复制一份再慢慢跑偏。
2. 唯一的区别是启动参数多一个 --runtime runsc，以及可用性判据多一条「runsc 必须已注册」。
3. runsc 不可用时 fail-closed：既不回落到 runc（那样隔离级别会静默降级），
   也不回落到 LocalRuntime（那样等于把「约束层」说成隔离）。

关于本机：Windows 上 gVisor 不可能运行（需要 Linux 内核）。所以本模块在本机永远报
unavailable，测试只验证「配置正确、判据正确、不谎报」，绝不把 Windows 上的结论
外推成「runsc 隔离已验证」。
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import platform
import shutil
import subprocess
import time
from typing import Any, Mapping, Sequence

from pq_agent.sandbox.capabilities import SUPPORTED, UNSUPPORTED, Capability
from pq_agent.sandbox.docker_runtime import (
    CAPABILITIES_SUPPORTED,
    CONTAINER_USER,
    CONTAINER_WORKDIR,
    DEFAULT_CPUS,
    DEFAULT_IMAGE,
    DEFAULT_MEMORY,
    DEFAULT_PIDS_LIMIT,
    DEFAULT_TMPFS_SIZE,
    DOCKER_BINARY,
    DockerProbe,
    DockerRuntime,
    MountSpec,
    probe_docker,
)

ISOLATION_LEVEL = "gvisor"
ISOLATION_LEVEL_NOTE = (
    "gVisor（runsc）在自己的用户态内核里实现系统调用：容器里的程序看到的是一层软件内核，"
    "只有被 gVisor 转发的调用才会到达宿主内核，宿主内核的暴露面比 runc 小一个数量级。"
    "但它仍然共享宿主内核，不是虚拟机；侧信道、部分系统调用的语义差异与兼容性缺口仍在。"
)

GVISOR_RUNTIME_NAME = "runsc"
RUNSC_BINARY = "runsc"

PLATFORM_KVM = "kvm"
PLATFORM_PTRACE = "ptrace"
KVM_DEVICE = "/dev/kvm"

# 云端暴露规则：服务只绑 localhost，笔记本走 SSH tunnel，绝不开放公网。
CLOUD_BIND_HOST = "127.0.0.1"
PUBLIC_EXPOSURE_ALLOWED = False
SSH_TUNNEL_REQUIRED = True
CLOUD_SERVICE_NOTE = (
    "云端 agent 服务只监听 127.0.0.1，笔记本通过 SSH 隧道访问（ssh -L 8123:127.0.0.1:8123）。"
    "Runner、OTel Collector、SkyWalking 面板一律不绑公网地址。"
)

# 基准测试的固定口径。数字必须可复现，所以这些值写进代码而不是写在报告里。
BENCH_WARMUP_RUNS = 3
BENCH_REPEAT_RUNS = 30
BENCH_SAMPLE_COUNT = 20
BENCH_RUNTIMES = ("runc", "runsc")
BENCH_METRICS = (
    "startup_ms_p50",
    "startup_ms_p95",
    "exec_ms_p50",
    "exec_ms_p95",
    "malicious_block_rate",
    "benign_pass_rate",
    "false_positive_rate",
    "timeout_rate",
    "leftover_process_count",
)
BENCH_TEMPLATE_PATH = "agent_eval/results/sandbox_bench.md"

# CentOS 生命周期日期，只用于在探测结果里提示「这套系统已停止维护」。
# 这是提示不是结论，部署前必须自己核对厂商公告。
CENTOS_EOL = {
    "7": "2024-06-30",
    "8": "2021-12-31",
    "9": "2027-05-31",
}
CENTOS_EOL_NOTE = "只登记已知生命周期日期，用于提示系统是否已停止维护，不代替厂商公告。"

# gVisor 在 Docker 那 12 条之外多出来的能力。
_GVISOR_EXTRA_SUPPORTED: tuple[tuple[str, str, str], ...] = (
    (
        "user_space_kernel",
        "系统调用由 gVisor 在用户态实现，程序面对的不是宿主内核接口。",
        "runsc 用户态内核（Sentry）",
    ),
    (
        "syscall_filtering",
        "未被 gVisor 实现的系统调用按平台策略拒绝或降级，而不是直接打到宿主内核。",
        "runsc 平台层（kvm / ptrace）",
    ),
    (
        "reduced_host_kernel_surface",
        "宿主内核只处理被转发的调用，暴露面比 runc 小；是减少不是消除。",
        "runsc + --runtime runsc",
    ),
)

_GVISOR_UNSUPPORTED: tuple[tuple[str, str, str], ...] = (
    (
        "kernel_side_channel_proof",
        "仍共享宿主内核：侧信道与部分系统调用语义差异无法在本层证明不存在，gVisor 不是虚拟机。",
        "",
    ),
)

# 与 LOCAL_CAPABILITIES、DOCKER_CAPABILITIES 各一份，绝不共用一张表。
GVISOR_CAPABILITIES: tuple[Capability, ...] = (
    tuple(
        Capability(
            key,
            SUPPORTED,
            "由 docker run 的加固参数强制（与 DockerRuntime 同一份实现）。",
            "docker run 加固参数",
        )
        for key in CAPABILITIES_SUPPORTED
    )
    + tuple(Capability(key, SUPPORTED, detail, enforced) for key, detail, enforced in _GVISOR_EXTRA_SUPPORTED)
    + tuple(Capability(key, UNSUPPORTED, detail, enforced) for key, detail, enforced in _GVISOR_UNSUPPORTED)
)


def gvisor_capability_matrix() -> dict[str, Capability]:
    """GVisorRuntime 的能力矩阵。"""
    return {item.key: item for item in GVISOR_CAPABILITIES}

@dataclasses.dataclass(frozen=True)
class HostFacts:
    """云端宿主探测的原始事实。每一条要么是实测出来的，要么是空值。"""

    system: str
    kernel_release: str
    distro_id: str
    distro_version: str
    distro_pretty: str
    centos_eol: str
    docker_probe: DockerProbe
    runsc_binary: str
    runsc_version: str
    runsc_registered: bool
    registered_runtimes: tuple[str, ...]
    kvm_present: bool
    kvm_readable: bool
    rootless: bool
    security_options: tuple[str, ...]
    probed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "kernel_release": self.kernel_release,
            "distro_id": self.distro_id,
            "distro_version": self.distro_version,
            "distro_pretty": self.distro_pretty,
            "centos_eol": self.centos_eol,
            "docker": self.docker_probe.to_dict(),
            "runsc_binary": self.runsc_binary,
            "runsc_version": self.runsc_version,
            "runsc_registered": self.runsc_registered,
            "registered_runtimes": list(self.registered_runtimes),
            "kvm_present": self.kvm_present,
            "kvm_readable": self.kvm_readable,
            "rootless": self.rootless,
            "security_options": list(self.security_options),
            "probed_at": self.probed_at,
        }


def _read_os_release() -> dict[str, str]:
    """读 /etc/os-release。读不到就返回空字典，绝不猜发行版。"""
    path = pathlib.Path("/etc/os-release")
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip(chr(34))
    return out


def _run_text(argv: Sequence[str], *, timeout: float = 20.0) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:  # noqa: BLE001 - 探测要把任何异常变成一句可读的失败原因
        return 127, type(exc).__name__ + ": " + str(exc)[:200]
    return int(done.returncode), (done.stdout or done.stderr or "").strip()


def _registered_runtimes(binary: str) -> tuple[str, ...]:
    """问 Docker 注册了哪些 runtime。问不出来就返回空元组，不假装注册过。"""
    if not binary:
        return ()
    code, text = _run_text([binary, "info", "--format", "{{json .Runtimes}}"])
    if code != 0 or not text:
        return ()
    try:
        data = json.loads(text)
    except ValueError:
        return ()
    if not isinstance(data, Mapping):
        return ()
    return tuple(sorted(str(key) for key in data.keys()))


def probe_host(
    *,
    binary: str = DOCKER_BINARY,
    runsc_binary: str = RUNSC_BINARY,
    environ: Mapping[str, str] | None = None,
) -> HostFacts:
    """一次性把云端宿主的事实全部量出来。

    刻意不走「只看 uname -r」那条路：内核版本说明不了 runsc 是否注册、
    /dev/kvm 是否可读、Docker 是否真的能用。这些必须一条条实测。
    """
    docker_probe = probe_docker(binary=binary)
    docker_path = docker_probe.binary if docker_probe.available else (shutil.which(binary) or "")
    runsc_path = shutil.which(runsc_binary) or ""
    runsc_version = ""
    if runsc_path:
        code, text = _run_text([runsc_path, "--version"])
        if code == 0:
            runsc_version = text.splitlines()[0] if text else ""
    runtimes = _registered_runtimes(docker_path)
    os_release = _read_os_release()
    distro_id = os_release.get("ID", "")
    distro_version = os_release.get("VERSION_ID", "")
    eol = CENTOS_EOL.get(distro_version, "") if distro_id == "centos" else ""
    kvm = pathlib.Path(KVM_DEVICE)
    kvm_present = kvm.exists()
    kvm_readable = bool(kvm_present and os.access(KVM_DEVICE, os.R_OK))
    src = environ if environ is not None else os.environ
    rootless = str(src.get("DOCKER_HOST", "") or "").startswith("unix:///run/user")
    security_options: tuple[str, ...] = ()
    if docker_probe.available:
        code, text = _run_text([docker_probe.binary, "info", "--format", "{{json .SecurityOptions}}"])
        if code == 0 and text:
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                security_options = tuple(str(item) for item in parsed)
    return HostFacts(
        system=platform.system(),
        kernel_release=platform.release(),
        distro_id=distro_id,
        distro_version=distro_version,
        distro_pretty=os_release.get("PRETTY_NAME", ""),
        centos_eol=eol,
        docker_probe=docker_probe,
        runsc_binary=runsc_path,
        runsc_version=runsc_version,
        runsc_registered=GVISOR_RUNTIME_NAME in runtimes,
        registered_runtimes=runtimes,
        kvm_present=kvm_present,
        kvm_readable=kvm_readable,
        rootless=rootless,
        security_options=security_options,
        probed_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )


def decide_platform(facts: HostFacts) -> str:
    """决定用 kvm 还是 ptrace 平台。任一条硬条件不满足就返回空串（fail-closed）。

    顺序就是优先级：不是 Linux、Docker 不可用、runsc 没注册，这三条任意一条成立，
    这一层就直接不可用——不允许「先拿 runc 顶上」。
    """
    if str(facts.system).lower() != "linux":
        return ""
    if not facts.docker_probe.available:
        return ""
    if not facts.runsc_registered:
        return ""
    if facts.kvm_present and facts.kvm_readable:
        return PLATFORM_KVM
    # /dev/kvm 不存在不是「不可用」：ptrace 平台不需要 KVM，只是慢。
    # 「存在却不可读」是权限问题，退回 ptrace 仍然成立，但必须让上层看见原因。
    return PLATFORM_PTRACE


def platform_blocker(facts: HostFacts) -> str:
    """返回「为什么这一层不可用」的第一手原因；可用时返回空串。"""
    if str(facts.system).lower() != "linux":
        return "宿主不是 Linux（" + str(facts.system) + "），gVisor 需要 Linux 内核"
    if not facts.docker_probe.available:
        return "Docker 不可用：" + facts.docker_probe.error
    if not facts.runsc_registered:
        registered = "、".join(facts.registered_runtimes) if facts.registered_runtimes else "未探到任何 runtime"
        return "Docker 未注册 runsc（已注册：" + registered + "）"
    return ""


def platform_note(facts: HostFacts) -> str:
    """可用时也要说清楚的注意事项（比如 KVM 不可读只能走 ptrace）。"""
    if facts.kvm_present and not facts.kvm_readable:
        return "/dev/kvm 存在但不可读，已选 ptrace 平台：隔离仍然成立，性能会下降"
    if not facts.kvm_present:
        return "未发现 /dev/kvm，已选 ptrace 平台：隔离仍然成立，性能会下降"
    return "已选 kvm 平台"


@dataclasses.dataclass(frozen=True)
class GvisorProbe:
    """gVisor 层的可用性判定结果，连同判定依据一起保存。"""

    available: bool
    platform: str
    runtime_name: str
    blocker: str
    note: str
    facts: HostFacts
    checked_at: str

    @property
    def docker_probe(self) -> DockerProbe:
        return self.facts.docker_probe

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "platform": self.platform,
            "runtime_name": self.runtime_name,
            "blocker": self.blocker,
            "note": self.note,
            "checked_at": self.checked_at,
            "facts": self.facts.to_dict(),
        }


def build_gvisor_probe(
    *,
    binary: str = DOCKER_BINARY,
    runsc_binary: str = RUNSC_BINARY,
    facts: HostFacts | None = None,
) -> GvisorProbe:
    """探测 + 判定一次做完。不可用的原因必须原样可读，不能吞掉。"""
    resolved = facts if facts is not None else probe_host(binary=binary, runsc_binary=runsc_binary)
    platform_name = decide_platform(resolved)
    blocker = "" if platform_name else platform_blocker(resolved)
    note = platform_note(resolved) if platform_name else ""
    return GvisorProbe(
        available=bool(platform_name),
        platform=platform_name,
        runtime_name=GVISOR_RUNTIME_NAME,
        blocker=blocker,
        note=note,
        facts=resolved,
        checked_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )

class GvisorRuntime(DockerRuntime):
    """第三层沙箱：与 DockerRuntime 同一套加固参数，只把容器运行时换成 runsc。

    继承而不是复制是刻意的：加固参数只有一份，审计逻辑只有一份，
    「gVisor 比 Docker 更安全」这件事就只体现在 --runtime runsc 这一个参数上，
    而审计脚本会逐条断言它确实在 argv 里。
    """

    name = "gvisor"
    display_name = "GVisorRuntime"
    isolation_level = ISOLATION_LEVEL
    isolation_level_note = ISOLATION_LEVEL_NOTE
    containment_subject = "gvisor_isolation"
    container_runtime = GVISOR_RUNTIME_NAME
    is_safety_isolation = True

    def __init__(
        self,
        *,
        root: pathlib.Path | str | None = None,
        image: str = DEFAULT_IMAGE,
        mounts: Sequence[MountSpec] = (),
        memory: str = DEFAULT_MEMORY,
        pids_limit: int = DEFAULT_PIDS_LIMIT,
        cpus: str = DEFAULT_CPUS,
        binary: str = DOCKER_BINARY,
        probe: GvisorProbe | None = None,
        facts: HostFacts | None = None,
        allow_untrusted: bool = False,
    ) -> None:
        self.gvisor_probe = (
            probe if probe is not None else build_gvisor_probe(binary=binary, facts=facts)
        )
        super().__init__(
            root=root,
            image=image,
            mounts=mounts,
            memory=memory,
            pids_limit=pids_limit,
            cpus=cpus,
            binary=binary,
            probe=self.gvisor_probe.docker_probe,
            allow_untrusted=allow_untrusted,
        )

    @property
    def available(self) -> bool:
        """可用性看 gVisor 探针，不看 Docker 探针：Docker 能用不等于 runsc 能用。"""
        return self.gvisor_probe.available

    @property
    def platform(self) -> str:
        return self.gvisor_probe.platform

    def capability_matrix(self) -> dict[str, Capability]:
        return gvisor_capability_matrix()

    def capabilities(self) -> dict[str, Any]:
        """gVisor 的能力清单。不可用时全部标 unsupported，不谎报 supported。"""
        status = "available" if self.available else "unavailable"
        reason = ("gVisor 可用（平台 " + self.platform + "）") if self.available else self.gvisor_probe.blocker
        supported = [item.key for item in GVISOR_CAPABILITIES if item.supported]
        unsupported = [item.key for item in GVISOR_CAPABILITIES if not item.supported]
        return {
            "runtime": type(self).__name__,
            "name": self.name,
            "isolation_level": self.isolation_level,
            "isolation_level_note": self.isolation_level_note,
            "status": status,
            "reason": reason,
            "image": self.image,
            "container_runtime": self.container_runtime,
            "platform": self.platform,
            "supported": supported if self.available else [],
            "unsupported": unsupported if self.available else supported + unsupported,
            "probe": self.gvisor_probe.to_dict(),
        }

    def unavailable_detail(self) -> str:
        return (
            self.display_name + " 不可用：" + (self.gvisor_probe.blocker or "未探到可用平台")
            + "。已拒绝执行：既不回落到 runc（那会让隔离级别静默降级），"
            + "也不用 LocalRuntime 冒充隔离。"
        )

    def gvisor_report(self) -> dict[str, Any]:
        """gVisor 层的完整报告：环境事实 + 判定 + 能力 gate + argv 审计。"""
        return {
            "runtime": type(self).__name__,
            "name": self.name,
            "isolation_level": self.isolation_level,
            "isolation_level_note": self.isolation_level_note,
            "available": self.available,
            "platform": self.platform,
            "container_runtime": self.container_runtime,
            "probe": self.gvisor_probe.to_dict(),
            "capabilities": self.capabilities(),
            "gate": self.containment_gate(),
            "audit": self.audit(),
            "exposure": {
                "bind_host": CLOUD_BIND_HOST,
                "public_exposure_allowed": PUBLIC_EXPOSURE_ALLOWED,
                "ssh_tunnel_required": SSH_TUNNEL_REQUIRED,
                "note": CLOUD_SERVICE_NOTE,
            },
        }


def gvisor_report(
    *,
    root: pathlib.Path | str | None = None,
    binary: str = DOCKER_BINARY,
    runsc_binary: str = RUNSC_BINARY,
    facts: HostFacts | None = None,
) -> dict[str, Any]:
    """模块级入口：不可用时也返回完整报告，只是 available 为 False。"""
    probe = build_gvisor_probe(binary=binary, runsc_binary=runsc_binary, facts=facts)
    runtime = GvisorRuntime(root=root, binary=binary, probe=probe)
    return runtime.gvisor_report()


def bench_plan() -> dict[str, Any]:
    """基准测试的固定口径。报告里的每个数字都必须能对应到这里的一项配置。"""
    return {
        "runtimes": list(BENCH_RUNTIMES),
        "warmup_runs": BENCH_WARMUP_RUNS,
        "repeat_runs": BENCH_REPEAT_RUNS,
        "sample_count": BENCH_SAMPLE_COUNT,
        "metrics": list(BENCH_METRICS),
        "image": DEFAULT_IMAGE,
        "memory": DEFAULT_MEMORY,
        "pids_limit": DEFAULT_PIDS_LIMIT,
        "cpus": DEFAULT_CPUS,
        "tmpfs_size": DEFAULT_TMPFS_SIZE,
        "user": CONTAINER_USER,
        "workdir": CONTAINER_WORKDIR,
        "template": BENCH_TEMPLATE_PATH,
    }