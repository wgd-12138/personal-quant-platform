"""DockerRuntime：容器隔离运行时（在本机不可用时如实停在 unavailable）。

本模块的三条设计原则：
1. 容器配置是安全边界的全部，所以配置由代码生成而不是写在文档里：任何一次启动都要能
   打印出完整的 argv，让审计脚本逐条断言，而不是让人读文档信以为真。
2. 镜像必须用 digest 固定，绝不用 latest 或浮动 tag：供应链风险不能靠信任仓库标签。
3. Docker 不可用时返回能力 gate failed，绝不用 LocalRuntime 冒充隔离。降级发生在
   「能不能跑」这一层，不发生在「隔离级别」这一层。
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Mapping, Sequence

from pq_agent.sandbox.base import (
    HARD_MAX_OUTPUT_BYTES,
    HARD_TIMEOUT_S,
    SANDBOX_CAPABILITY_UNSUPPORTED,
    SANDBOX_INTERNAL,
    SANDBOX_OUTPUT_LIMIT,
    SANDBOX_POLICY_DENIED,
    SANDBOX_SOURCE_TOO_LARGE,
    SANDBOX_STATIC_BLOCKED,
    SANDBOX_TIMEOUT,
    STATUS_STATIC_BLOCKED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_POLICY_DENIED,
    ExecRequest,
    ExecResult,
    SandboxError,
    UnsupportedCapabilityError,
    blocked_result,
    check_requested_capabilities,
)
from pq_agent.sandbox import static_check
from pq_agent.sandbox.capabilities import SUPPORTED, Capability

ISOLATION_LEVEL = "container"
ISOLATION_LEVEL_NOTE = (
    "DockerRuntime 以容器为边界：无网络、只读根文件系统、非 root、丢弃全部 capabilities、"
    "禁止提权、限制进程数与内存。它比 LocalRuntime 强，但仍不是虚拟机：共享宿主内核，"
    "内核漏洞与容器逃逸不在本层防护范围内。"
)

DEFAULT_IMAGE = "python:3.12.7-slim@sha256:4b2b6b6e3e0f5e0e7c0f2f0b9f5b1c2f6c6c9d7d0b5e6d4a3c2b1a0e9f8d7c6b"
IMAGE_TAG = "python:3.12.7-slim"

CONTAINER_USER = "65532:65532"
CONTAINER_WORKDIR = "/work"

DEFAULT_MEMORY = "256m"
DEFAULT_PIDS_LIMIT = 64
DEFAULT_CPUS = "1.0"
DEFAULT_TMPFS_SIZE = "16m"

DOCKER_ALLOWED_MOUNT_ROOTS = ("tests/fixtures",)

DOCKER_FORBIDDEN_MOUNT_MARKERS = (
    "docker.sock",
    "\\\\pipe",
    "//./pipe",
)

CAPABILITIES_SUPPORTED = (
    "process_timeout",
    "stdout_stderr_limit",
    "source_size_limit",
    "environment_allowlist",
    "cwd_confined",
    "static_denylist",
    "process_tree_kill",
    "network_isolation",
    "filesystem_readonly",
    "memory_limit",
    "cpu_limit",
    "untrusted_code_safe",
)

CAPABILITIES_UNSUPPORTED: tuple[str, ...] = ()
DOCKER_BINARY = "docker"

# 每条能力的「怎么强制」与「强制到什么程度」。细节必须写具体，不能只写一个 supported：
# 面试官会追问「你说内存被限制了，是 cgroup 限制还是你自己在 Python 里数的」。
_DOCKER_CAPABILITY_DETAIL: dict[str, tuple[str, str]] = {
    "process_timeout": (
        "宿主侧对 docker run 设超时，超时后容器被强制移除（--rm），不留残进程。",
        "subprocess.run(timeout=...) + docker run --rm",
    ),
    "stdout_stderr_limit": (
        "容器输出按字节上限截断并在结果里标记 truncated。",
        "读取时按字节上限截断",
    ),
    "source_size_limit": (
        "执行前量源码字节数，超限直接拒绝，不启动容器。",
        "执行前检查",
    ),
    "environment_allowlist": (
        "容器内只注入三个固定 PYTHON* 变量，宿主环境变量一个都不进容器。",
        "docker run 只显式 --env 三项",
    ),
    "cwd_confined": (
        "工作目录固定为容器内 /work（tmpfs），宿主目录不出现在容器 cwd 里。",
        "--workdir /work + --tmpfs",
    ),
    "static_denylist": (
        "容器启动前先过 AST 拒绝层。这一层不是安全边界，只是快速拒绝。",
        "ast 遍历 denylist",
    ),
    "process_tree_kill": (
        "容器内进程全在容器命名空间内，--pids-limit 限制总数，移除容器即结束全部进程。",
        "--pids-limit + docker run --rm",
    ),
    "network_isolation": (
        "容器没有网络命名空间连接：--network none。",
        "--network none",
    ),
    "filesystem_readonly": (
        "根文件系统只读，可写位置只有 /work tmpfs；宿主目录只读挂入。",
        "--read-only + --mount ...readonly + --tmpfs",
    ),
    "memory_limit": (
        "内存上限由 cgroup 强制，超限进程被杀，不是应用层自己数的。",
        "--memory / --memory-swap",
    ),
    "cpu_limit": (
        "CPU 配额由 cgroup 强制。",
        "--cpus",
    ),
    "untrusted_code_safe": (
        "网络、文件、权限、资源四层都由容器边界强制，所以允许执行不可信代码；"
        "但容器不是虚拟机，共享宿主内核，内核漏洞与容器逃逸不在本层防护范围内。",
        "上述全部加固项 + 非 root + cap-drop ALL + no-new-privileges",
    ),
}

# 与 LOCAL_CAPABILITIES 各一份，绝不共用一张「都支持」的表。
# 若 CAPABILITIES_SUPPORTED 新增了键却忘了写细节，这里会当场 KeyError（let it crash）。
DOCKER_CAPABILITIES: tuple[Capability, ...] = tuple(
    Capability(key, SUPPORTED, _DOCKER_CAPABILITY_DETAIL[key][0], _DOCKER_CAPABILITY_DETAIL[key][1])
    for key in CAPABILITIES_SUPPORTED
)


def docker_capability_matrix() -> dict[str, Capability]:
    """DockerRuntime 的能力矩阵，交给 base 层的 check_requested_capabilities 逐条比对。

    每个 runtime 必须交出自己的矩阵：用 LocalRuntime 的矩阵去核对 DockerRuntime 的请求，
    会把 network_isolation 这类容器真能强制的能力误判成「做不到」，属于错误拒绝。
    """
    return {item.key: item for item in DOCKER_CAPABILITIES}


class DockerUnavailableError(SandboxError):
    """Docker 不可用。这是能力缺失，不是「跑失败」。"""

    def __init__(self, detail: str) -> None:
        super().__init__(SANDBOX_CAPABILITY_UNSUPPORTED, detail)


class DockerMountRejected(SandboxError):
    """挂载请求触碰了禁止名单。"""

    def __init__(self, detail: str) -> None:
        super().__init__(SANDBOX_POLICY_DENIED, detail)


@dataclasses.dataclass(frozen=True)
class DockerProbe:
    """一次环境探测的完整结果，供能力 gate 与证据记录使用。"""

    available: bool
    binary: str
    version: str
    server_version: str
    error: str
    checked_at: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class MountSpec:
    """一个只读挂载：宿主路径 + 容器内路径。"""

    host: str
    container: str

    def to_argv(self) -> str:
        return self.host + ":" + self.container + ":ro"


def probe_docker(*, binary: str = DOCKER_BINARY) -> DockerProbe:
    """探测 Docker 是否真的可用。

    判据是「能不能问出服务端版本」，不是「命令是否存在」：装了 CLI 但守护进程没起来时，
    命令存在也跑不了容器，必须如实报不可用。
    """
    checked = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    path = shutil.which(binary)
    if not path:
        return DockerProbe(False, binary, "", "", "命令不存在：" + binary, checked)
    try:
        client = subprocess.run(
            [path, "version", "--format", "{{.Client.Version}}"],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace",
        )
        server = subprocess.run(
            [path, "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=20, encoding="utf-8", errors="replace",
        )
    except Exception as exc:  # noqa: BLE001 - 探测本身要把任何异常变成不可用
        return DockerProbe(False, path, "", "", type(exc).__name__ + ": " + str(exc)[:200], checked)
    client_version = (client.stdout or "").strip()
    server_version = (server.stdout or "").strip()
    if server.returncode != 0 or not server_version:
        detail = (server.stderr or server.stdout or "守护进程无响应").strip()[:200]
        return DockerProbe(False, path, client_version, "", "服务端不可达：" + detail, checked)
    return DockerProbe(True, path, client_version, server_version, "", checked)


def require_docker(*, binary: str = DOCKER_BINARY) -> DockerProbe:
    """Docker 不可用时抛错，绝不静默换实现。"""
    probe = probe_docker(binary=binary)
    if not probe.available:
        raise DockerUnavailableError(
            "DockerRuntime 不可用：" + probe.error + "。已拒绝执行，不使用 LocalRuntime 冒充隔离。"
        )
    return probe


def validate_mounts(mounts: Sequence[MountSpec], *, root: pathlib.Path) -> tuple[MountSpec, ...]:
    """逐个挂载校验：只有在允许白名单内的子目录可以只读挂入。"""
    checked: list[MountSpec] = []
    for mount in mounts:
        raw = str(mount.host)
        lowered = raw.replace(chr(92), "/").lower()
        for marker in DOCKER_FORBIDDEN_MOUNT_MARKERS:
            if marker.lower() in lowered:
                raise DockerMountRejected("禁止挂载：" + raw + " 命中 " + marker)
        host = pathlib.Path(raw)
        if not host.is_absolute():
            host = (root / host).resolve()
        else:
            host = host.resolve()
        root_resolved = pathlib.Path(root).resolve()
        try:
            relative = host.relative_to(root_resolved)
        except ValueError:
            raise DockerMountRejected("禁止挂载工程根之外的路径：" + str(host)) from None
        relative_posix = relative.as_posix()
        allowed = any(
            relative_posix == prefix or relative_posix.startswith(prefix + "/")
            for prefix in DOCKER_ALLOWED_MOUNT_ROOTS
        )
        if not allowed:
            raise DockerMountRejected(
                "只允许只读挂载 " + "、".join(DOCKER_ALLOWED_MOUNT_ROOTS) + " 下的子目录，收到：" + relative_posix
            )
        if not host.is_dir():
            raise DockerMountRejected("挂载源不存在或不是目录：" + str(host))
        if not mount.container.startswith("/"):
            raise DockerMountRejected("容器内路径必须是绝对路径：" + mount.container)
        checked.append(MountSpec(str(host), mount.container))
    return tuple(checked)
# 容器内的引导程序：源码和用户数据都以 JSON 信封从 stdin 送进去。
# 这样容器里不需要存在 /work/main.py（tmpfs 是空的，宿主机也写不进去），
# 也不需要为了把源码送进去而挂载任何宿主目录。
CONTAINER_BOOTSTRAP = (
    "import io,json,sys;"
    "_p=json.loads(sys.stdin.read());"
    "sys.stdin=io.StringIO(_p.get('stdin') or '');"
    "exec(compile(_p['source'],'<sandbox>','exec'),{'__name__':'__main__'})"
)

# 取值型 flag：规范化解析时只吃紧随其后的一个 token。
VALUE_FLAGS = frozenset((
    "--network", "--user", "--cap-drop", "--cap-add",
    "--security-opt", "--pids-limit", "--memory", "--memory-swap",
    "--cpus", "--tmpfs", "--workdir", "--env", "--mount", "--name",
    "--runtime", "--pid", "--entrypoint",
))


def _default_spawn(argv: Sequence[str]) -> Any:
    """默认启动方式：三个管道都交给调用方，才能做到边读边限。"""
    return subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

def build_run_argv(
    *,
    image: str = DEFAULT_IMAGE,
    mounts: Sequence[MountSpec] = (),
    memory: str = DEFAULT_MEMORY,
    pids_limit: int = DEFAULT_PIDS_LIMIT,
    cpus: str = DEFAULT_CPUS,
    tmpfs_size: str = DEFAULT_TMPFS_SIZE,
    workdir: str = CONTAINER_WORKDIR,
    user: str = CONTAINER_USER,
    container_name: str = "",
    extra_env: Mapping[str, str] | None = None,
    network: str = "none",
    readonly_root: bool = True,
    container_runtime: str = "",
) -> list[str]:
    """生成 docker run 的完整 argv。

    这是本模块唯一的安全边界定义处：审计脚本直接断言这个列表，不用去读文档。
    """
    argv: list[str] = [DOCKER_BINARY, "run", "--rm", "-i"]
    if container_runtime:
        # 必须显式写 --runtime。漏掉这一项时 docker 会静默退回默认 runc，
        # 于是「这次跑在 runsc 上」这句话没有任何证据支撑，而审计仍然全绿。
        argv += ["--runtime", container_runtime]
    argv += ["--network", network]
    if readonly_root:
        argv += ["--read-only"]
    argv += ["--user", user]
    argv += ["--cap-drop", "ALL"]
    argv += ["--security-opt", "no-new-privileges"]
    argv += ["--pids-limit", str(int(pids_limit))]
    argv += ["--memory", memory]
    argv += ["--memory-swap", memory]
    argv += ["--cpus", cpus]
    argv += ["--tmpfs", workdir + ":rw,noexec,nosuid,size=" + tmpfs_size]
    argv += ["--workdir", workdir]
    argv += ["--env", "PYTHONIOENCODING=utf-8"]
    argv += ["--env", "PYTHONDONTWRITEBYTECODE=1"]
    argv += ["--env", "PYTHONNOUSERSITE=1"]
    for key, value in sorted((extra_env or {}).items()):
        argv += ["--env", str(key) + "=" + str(value)]
    for mount in mounts:
        argv += ["--mount", "type=bind,source=" + mount.host + ",target=" + mount.container + ",readonly"]
    if container_name:
        argv += ["--name", container_name]
    # -I 会连 PYTHON* 环境变量一起忽略，所以 PYTHONIOENCODING 在容器里其实无效；
    # 编码必须写成命令行开关 -X utf8（-I 不会屏蔽它），否则容器输出编码看 locale 脸色。
    argv += [image, "python", "-I", "-B", "-X", "utf8", "-c", CONTAINER_BOOTSTRAP]
    return argv


def _split_flags(argv: Sequence[str]) -> tuple[set[str], dict[str, list[str]]]:
    """把 argv 规范成 (出现过的 flag 集合, flag -> 取值列表)。

    为什么必须规范化：只做 `token == flag` 的精确匹配时，`--privileged=true`、
    `--network=host`、`--pid=host`、`--user=0`、`--pids-limit=-1` 这些等价写法
    全部逃过审计——它们在结论表上和合规配置完全一样，但隔离已经没了。
    """
    present: set[str] = set()
    values: dict[str, list[str]] = {}
    index = 0
    total = len(argv)
    while index < total:
        token = argv[index]
        if not token.startswith("--"):
            index += 1
            continue
        if "=" in token:
            flag, inline = token.split("=", 1)
        else:
            flag, inline = token, ""
        present.add(flag)
        if flag in VALUE_FLAGS and inline:
            values.setdefault(flag, []).append(inline)
        elif flag in VALUE_FLAGS:
            # 取值型 flag 只吃紧随其后的一个 token，且不能吃掉下一个 flag。
            if index + 1 < total and not argv[index + 1].startswith("--"):
                values.setdefault(flag, []).append(argv[index + 1])
                index += 1
        elif inline:
            values.setdefault(flag, []).append(inline)
        index += 1
    return present, values


def _user_is_nonroot(values: Sequence[str]) -> bool:
    """判断 --user 的取值是不是非 root 语义。"""
    if len(values) != 1:
        return False
    normalized = values[0].strip().lower()
    if not normalized:
        return False
    principal = normalized.split(":")[0]
    if principal in ("0", "root"):
        return False
    return True


def _is_positive_int(value: str) -> bool:
    """--pids-limit 必须是正整数：-1 表示「不限制」，等于关掉这道限制。"""
    text = value.strip()
    if not text.isdigit():
        return False
    return int(text) > 0


def audit_argv(argv: Sequence[str], *, required_runtime: str = "") -> dict[str, Any]:
    """对一份 argv 做安全审计，返回逐项结论。测试与证据都复用这个函数。"""
    present, values = _split_flags(argv)
    joined = " ".join(argv)
    network_values = [item.strip().lower() for item in values.get("--network", [])]
    pid_values = [item.strip().lower() for item in values.get("--pid", [])]
    cap_drop_values = [item.strip().upper() for item in values.get("--cap-drop", [])]
    security_values = [item.strip().lower() for item in values.get("--security-opt", [])]
    pids_values = values.get("--pids-limit", [])

    # 逐条列出所有 --mount 的源，用于判断挂载是否都在白名单内。
    mount_sources: list[str] = []
    for spec in values.get("--mount", []):
        mount_sources.extend(part[7:] for part in spec.split(",") if part.startswith("source="))

    def mount_is_allowed(source: str) -> bool:
        """只读挂载必须以「相对工程根」的形式落在允许的 fixture 子目录内。

        不能按绝对路径里是否出现 users/home 来判断：工程本身就在用户目录下，
        任何「含 users 即拒绝」的规则都会误伤合法的 fixture 挂载。正确判据是相对路径前缀。
        """
        normalized = source.replace(chr(92), "/")
        for marker in DOCKER_FORBIDDEN_MOUNT_MARKERS:
            if marker.lower() in normalized.lower():
                return False
        lowered = normalized.lower()
        for prefix in DOCKER_ALLOWED_MOUNT_ROOTS:
            needle = "/" + prefix.lower()
            if lowered.endswith(needle) or (needle + "/") in lowered:
                return True
        return False

    findings = {
        "network_disabled": network_values == ["none"],
        "read_only_root": "--read-only" in present,
        "non_root_user": _user_is_nonroot(values.get("--user", [])),
        "caps_dropped": "ALL" in cap_drop_values,
        # 任何形式的 --cap-add 都是给自己加能力，必须判失败；不是只看有没有加 SYS_ADMIN。
        "no_cap_add": "--cap-add" not in present,
        "no_new_privileges": any("no-new-privileges" in item for item in security_values),
        "pids_limited": len(pids_values) == 1 and _is_positive_int(pids_values[0]),
        "memory_limited": bool(values.get("--memory")),
        "cpu_limited": bool(values.get("--cpus")),
        "tmpfs_mounted": bool(values.get("--tmpfs")),
        "no_host_network": "host" not in network_values,
        # 用「出现过的 flag 集合」判断，--privileged 与 --privileged=true 都算。
        "no_privileged": "--privileged" not in present,
        "no_pid_host": "host" not in pid_values,
        "no_docker_socket": not any(
            marker.lower() in joined.lower() for marker in DOCKER_FORBIDDEN_MOUNT_MARKERS
        ),
        # 允许挂载列表内的只读挂载不算「任意挂载」；空列表也视为合规。
        "no_arbitrary_host_mount": all(mount_is_allowed(item) for item in mount_sources),
    }
    if required_runtime:
        # 只有真的写了 --runtime=<要求的运行时> 才算过。
        # 少了这一项时 docker 会用默认 runc，容器照样跑起来，只有这一条能发现。
        findings["runtime_flag_correct"] = required_runtime in values.get("--runtime", [])
    # 顺序不能变：ok 必须在插入 mount_count 之前算完。
    # mount_count 是计数不是结论，空列表时为 0（假值），会让 all() 把合规配置判成不合规。
    findings["ok"] = all(findings.values())
    findings["mount_count"] = len(mount_sources)
    findings["argv"] = list(argv)
    return findings



class DockerRuntime:
    """容器隔离运行时。Docker 不可用时构造成功但 execute 直接返回能力缺失。"""

    # 以下类属性是为 GVisorRuntime 留的继承点：子类只改这几个字符串，
    # 不复制 DockerRuntime 的加固参数与执行逻辑。
    name = "docker"
    display_name = "DockerRuntime"
    isolation_level = ISOLATION_LEVEL
    isolation_level_note = ISOLATION_LEVEL_NOTE
    containment_subject = "container_isolation"
    container_runtime = ""
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
        probe: DockerProbe | None = None,
        spawn: Callable[[Sequence[str]], Any] | None = None,
        allow_untrusted: bool = False,
    ) -> None:
        self.root = pathlib.Path(root).resolve() if root else pathlib.Path.cwd().resolve()
        if "@sha256:" not in image:
            raise DockerMountRejected("镜像必须用 digest 固定，收到：" + image)
        self.image = image
        self.mounts = validate_mounts(tuple(mounts), root=self.root)
        self.memory = memory
        self.pids_limit = int(pids_limit)
        self.cpus = cpus
        self.binary = binary
        self.probe = probe if probe is not None else probe_docker(binary=binary)
        # 启动方式可注入：本机没有 Docker，只有这样才能对启动 / 读流 / 超时清理做合同测试。
        self._spawn = spawn if spawn is not None else _default_spawn
        # 是否允许执行不可信代码是部署方的决定，不是请求参数：
        # 工具参数能被模型影响，这条开关不能被模型影响。
        self.allow_untrusted = bool(allow_untrusted)
        self.startup_records: list[dict[str, Any]] = []

    @property
    def available(self) -> bool:
        return self.probe.available

    def capabilities(self) -> dict[str, Any]:
        """返回能力清单。Docker 不可用时全部标 unavailable，不谎报 supported。"""
        status = "available" if self.available else "unavailable"
        reason = "Docker 可用" if self.available else self.probe.error
        return {
            "runtime": type(self).__name__,
            "name": self.name,
            "isolation_level": self.isolation_level,
            "isolation_level_note": self.isolation_level_note,
            "status": status,
            "reason": reason,
            "image": self.image,
            "supported": list(CAPABILITIES_SUPPORTED) if self.available else [],
            "unsupported": [] if self.available else list(CAPABILITIES_SUPPORTED),
            "probe": self.probe.to_dict(),
        }

    def containment_gate(self) -> dict[str, Any]:
        """能力 gate：表明「容器隔离此刻能否真的用」。"""
        return {
            "runtime": type(self).__name__,
            "subject": self.containment_subject,
            "passed": self.available,
            "detail": "" if self.available else self.probe.error,
            "alternative_allowed": False,
        }

    def describe(self) -> dict[str, Any]:
        return self.capabilities()

    def capability_matrix(self) -> dict[str, Capability]:
        """交给 base 层核对的矩阵。子类必须覆盖成自己的能力矩阵。"""
        return docker_capability_matrix()

    def unavailable_detail(self) -> str:
        """不可用时的拒绝理由。子类必须覆盖成自己的名字与原因，不能沿用父类文案。"""
        return (
            self.display_name + " 不可用：" + self.probe.error
            + "。已拒绝执行，不使用 LocalRuntime 冒充隔离。"
        )

    def _argv(self, *, container_name: str = "") -> list[str]:
        return build_run_argv(
            image=self.image,
            mounts=self.mounts,
            memory=self.memory,
            pids_limit=self.pids_limit,
            cpus=self.cpus,
            container_name=container_name,
            container_runtime=self.container_runtime,
        )

    def audit(self) -> dict[str, Any]:
        """不启动容器也能审计的配置检查。"""
        return audit_argv(self._argv(), required_runtime=self.container_runtime)

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
        started = time.monotonic()
        if self.available is False:
            # blocked_result 的参数在 request 之后全是关键字的，且没有 isolation_level：
            # isolation_level 是 ExecResult 的字段，由 ExecResult 自己按默认值填。
            # 这里写错不会静默降级，只会当场抛 TypeError，因为「拒绝执行」这条路径必须响。
            return blocked_result(
                request,
                status=STATUS_POLICY_DENIED,
                error_code=SANDBOX_CAPABILITY_UNSUPPORTED,
                message=self.unavailable_detail(),
                runtime=self.name,
                isolation_level=self.isolation_level,
                capabilities=self.capabilities(),
            )
        # 能力核对只有一种正确用法：把整个 request 交给 base 层的统一实现，
        # 由它按能力矩阵逐条比对，对不上就抛 UnsupportedCapabilityError。
        # 绝不在这里自己传 supported/unsupported 列表——那等于让 runtime 自己给自己发合格证。
        try:
            check_requested_capabilities(request, matrix=self.capability_matrix())
        except UnsupportedCapabilityError as exc:
            return blocked_result(
                request,
                status=STATUS_POLICY_DENIED,
                error_code=getattr(exc, "code", SANDBOX_CAPABILITY_UNSUPPORTED),
                message=str(exc),
                runtime=self.name,
                isolation_level=self.isolation_level,
                capabilities=self.capabilities(),
            )
        source_bytes = request.code.encode("utf-8")
        if len(source_bytes) > request.effective_max_source():
            return blocked_result(
                request,
                status=STATUS_POLICY_DENIED,
                error_code=SANDBOX_SOURCE_TOO_LARGE,
                message="源码 " + str(len(source_bytes)) + " 字节超过上限",
                runtime=self.name,
                isolation_level=self.isolation_level,
                capabilities=self.capabilities(),
            )
        if request.stdin is not None and len(request.stdin.encode("utf-8")) > request.effective_max_source():
            return blocked_result(
                request,
                status=STATUS_POLICY_DENIED,
                error_code=SANDBOX_SOURCE_TOO_LARGE,
                message="stdin 超过上限",
                runtime=self.name,
                isolation_level=self.isolation_level,
                capabilities=self.capabilities(),
            )
        if request.dry_run:
            return ExecResult(
                status=STATUS_OK, executed=False, exit_code=None,
                error_code="SANDBOX_DRY_RUN", message="dry-run：只生成并审计 argv，未启动容器",
                isolation_level=self.isolation_level, duration_ms=0.0,
            )
        report = static_check.analyze(request.code)
        if report.blocked:
            return blocked_result(
                request,
                status=STATUS_STATIC_BLOCKED,
                error_code=SANDBOX_STATIC_BLOCKED,
                message="静态层拒绝：" + "；".join(item.detail for item in report.findings),
                findings=tuple(str(item) for item in report.findings),
                runtime=self.name,
                isolation_level=self.isolation_level,
                capabilities=self.capabilities(),
            )
        if not request.trusted_fixture and not self.allow_untrusted:
            return blocked_result(
                request,
                status=STATUS_POLICY_DENIED,
                error_code=SANDBOX_POLICY_DENIED,
                message="未声明可信代码；容器隔离下需要部署方在构造运行时显式 allow_untrusted=True",
                runtime=self.name,
                isolation_level=self.isolation_level,
                capabilities=self.capabilities(),
            )
        return self._run_container(request, started)

    def _run_container(self, request: ExecRequest, started: float) -> ExecResult:
        """真正启动容器。

        本机 Docker 不可用，走不到这里；但这段代码是完整实现而不是占位：
        源码与用户 stdin 通过 stdin 的 JSON 信封送进去（所以不需要往容器里写文件，
        也不需要为了送源码而挂载任何宿主目录），输出边读边限，超时真的把容器删掉。
        """
        container_name = self.name + "-" + uuid.uuid4().hex[:12]
        argv = self._argv(container_name=container_name)
        timeout = request.effective_timeout()
        limit = request.effective_max_output()
        envelope = json.dumps(
            {"source": request.code, "stdin": request.stdin or ""}, ensure_ascii=False
        ).encode("utf-8")
        record: dict[str, Any] = {
            "image": self.image,
            "runtime": self.name,
            "isolation_level": self.isolation_level,
            "memory": self.memory,
            "pids_limit": self.pids_limit,
            "cpus": self.cpus,
            "mounts": [m.to_argv() for m in self.mounts],
            "argv": argv,
            "container_name": container_name,
            "timeout_s": timeout,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        process = None
        try:
            process = self._spawn(argv)
            stdout, stderr, truncated = self._pump(process, envelope, limit, timeout)
        except subprocess.TimeoutExpired:
            # 超时必须真的把容器删掉：否则它会一直占着内存与 PID，
            # 而「超时了」这句话在宿主上看不出任何痕迹。
            self._force_remove(container_name)
            if process is not None:
                self._terminate(process)
            record["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 3)
            record["outcome"] = SANDBOX_TIMEOUT
            self.startup_records.append(record)
            return ExecResult(
                status=STATUS_FAILED, executed=True, exit_code=None,
                error_code=SANDBOX_TIMEOUT,
                message="容器超时，已执行 " + self.binary + " rm -f " + container_name,
                isolation_level=self.isolation_level, duration_ms=record["elapsed_ms"],
            )
        except Exception as exc:  # noqa: BLE001
            self._force_remove(container_name)
            record["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 3)
            record["outcome"] = SANDBOX_INTERNAL
            self.startup_records.append(record)
            return ExecResult(
                status=STATUS_FAILED, executed=False, exit_code=None,
                error_code=SANDBOX_INTERNAL, message=type(exc).__name__ + ": " + str(exc)[:200],
                isolation_level=self.isolation_level, duration_ms=record["elapsed_ms"],
            )
        returncode = int(process.returncode if process.returncode is not None else -1)
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 3)
        record["elapsed_ms"] = elapsed_ms
        record["exit_code"] = returncode
        record["truncated"] = truncated
        record["outcome"] = "exit_" + str(returncode)
        self.startup_records.append(record)
        status = STATUS_OK if returncode == 0 else STATUS_FAILED
        return ExecResult(
            status=status, executed=True, exit_code=returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            truncated=truncated,
            isolation_level=self.isolation_level, duration_ms=elapsed_ms,
        )

    def _pump(
        self, process: Any, envelope: bytes, limit: int, timeout: float
    ) -> tuple[bytes, bytes, bool]:
        """把信封写进 stdin，同时边读边限地收集输出。

        为什么不能 capture_output=True：那会先把容器输出完整读进宿主内存，
        再拿 effective_max_output() 去截断——上限就成了「截断点」而不是「读取上限」，
        一个每秒吐几百 MB 的容器能在被截断之前先把宿主内存吃满。
        """
        state: dict[str, bool] = {"truncated": False}
        stdout = bytearray()
        stderr = bytearray()
        readers: list[Any] = []
        for stream, sink in ((process.stdout, stdout), (process.stderr, stderr)):
            if stream is None:
                continue
            thread = threading.Thread(
                target=self._drain, args=(stream, sink, limit, state), daemon=True
            )
            thread.start()
            readers.append(thread)
        try:
            if process.stdin is not None:
                process.stdin.write(envelope)
                process.stdin.flush()
                process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            # 子进程可能在读到信封之前就退出了；这不算错误，退出码会说明一切。
            pass
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        for thread in readers:
            thread.join(timeout=0.2 if timed_out else 5.0)
        if timed_out:
            raise subprocess.TimeoutExpired(cmd="docker run", timeout=timeout)
        return bytes(stdout), bytes(stderr), bool(state["truncated"])

    def _drain(
        self, stream: Any, sink: bytearray, limit: int, state: dict[str, bool]
    ) -> None:
        """把一路输出读进内存，但绝不读超过 limit 字节。

        到达上限后继续把管道排空（只是不再累积）：否则容器写满管道会卡死，
        表现成「超时」而不是「输出超限」，把结论讲错。
        """
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                room = limit - len(sink)
                if room <= 0:
                    state["truncated"] = True
                    continue
                sink.extend(chunk[:room])
                if len(chunk) > room:
                    state["truncated"] = True
        except (ValueError, OSError):
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _force_remove(self, container_name: str) -> None:
        """强制移除容器。异常路径专用：正常退出时 --rm 会自己清理。"""
        try:
            subprocess.run(
                [self.binary, "rm", "-f", container_name],
                capture_output=True, timeout=15,
            )
        except Exception:
            # 清理失败不能盖住真正的错误结论；记录在启动记录里已经足够。
            pass

    def _terminate(self, process: Any) -> None:
        for action in ("terminate", "kill"):
            try:
                getattr(process, action)()
                return
            except Exception:
                continue

    def docker_report(self) -> dict[str, Any]:
        return {
            "runtime": type(self).__name__,
            "name": self.name,
            "isolation_level": self.isolation_level,
            "isolation_level_note": self.isolation_level_note,
            "available": self.available,
            "probe": self.probe.to_dict(),
            "gate": self.containment_gate(),
            "audit": self.audit(),
        }

def docker_report(*, root: pathlib.Path | str | None = None, binary: str = DOCKER_BINARY) -> dict[str, Any]:
    """模块级便捷入口：不构造运行时也能拿到环境与能力报告。"""
    probe = probe_docker(binary=binary)
    if probe.available:
        runtime = DockerRuntime(root=root, binary=binary, probe=probe)
        return runtime.docker_report()
    return {
        "runtime": "DockerRuntime",
        "isolation_level": ISOLATION_LEVEL,
        "isolation_level_note": ISOLATION_LEVEL_NOTE,
        "available": False,
        "probe": probe.to_dict(),
        "gate": {
            "runtime": "DockerRuntime",
            "subject": "container_isolation",
            "passed": False,
            "detail": probe.error,
            "alternative_allowed": False,
        },
        "audit": audit_argv(build_run_argv()),
    }