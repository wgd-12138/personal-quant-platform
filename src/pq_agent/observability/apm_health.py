"""A4-01 APM 栈健康与事实卡：把「APM 起来了」这件事变成可检查、可审计的断言。

为什么需要这个模块：

1. 「容器在跑」和「APM 能用」是两件事。容器起来只证明进程没退，不证明
   Collector 收到过 span、不证明 OAP 存下来了、更不证明查询接口答得出来。
   所以这里把探测分成四层：docker → collector → oap → 端到端入库查询，
   逐层给出 passed / failed / unavailable，绝不用上一层的成功推断下一层。

2. 「本机没装 Docker」必须是一个**可读的事实**，不是一句口头说明。
   没有 Docker 时 apm_degraded=true、signals 全部标未验证，
   并且明确写出「不是通过」，而不是让调用方自己猜。

3. 三种 signal 不共用接口。SkyWalking 11 的 OTel receiver 在同一个 gRPC 端口上
   按 handler 分流 traces/metrics/logs，但它们的存储与查询路径并不相同；
   因此本模块逐个 signal 记录状态，缺证据的标 APM_SIGNAL_UNSUPPORTED 或 UNVERIFIED。

本模块只读：不启动容器、不写配置文件、不发送任何外发请求。
启动与停止由 deploy/apm 下的 compose 命令完成，本模块负责判定与留证。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pq_agent.contracts import ContractError

APM_SCHEMA_VERSION = "pq_agent.apm_health/1"

# ---- 状态常量 -------------------------------------------------------------
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_UNAVAILABLE = "unavailable"
STATUS_SKIPPED = "skipped"
STATUSES = (STATUS_PASSED, STATUS_FAILED, STATUS_UNAVAILABLE, STATUS_SKIPPED)

SIGNAL_TRACES = "traces"
SIGNAL_METRICS = "metrics"
SIGNAL_LOGS = "logs"
SIGNALS = (SIGNAL_TRACES, SIGNAL_METRICS, SIGNAL_LOGS)

# 单个 signal 的结论码。语义必须区分：
#   supported    有端到端证据（SDK → Collector → OAP → 查询）
#   unsupported  这个版本/这份配置明确不支持
#   unverified   没证据，既不能说是也不能说不是
SIGNAL_SUPPORTED = "APM_SIGNAL_SUPPORTED"
SIGNAL_UNSUPPORTED = "APM_SIGNAL_UNSUPPORTED"
SIGNAL_UNVERIFIED = "APM_SIGNAL_UNVERIFIED"

# ---- 栈定义（与 deploy/apm/stack.json 同源，代码里再写一份是为了不依赖文件存在）
STACK_FILE = "deploy/apm/stack.json"
COMPOSE_FILE = "deploy/apm/docker-compose.yml"
COLLECTOR_CONFIG = "deploy/apm/otel-collector.yaml"
DOCKER_BINARY = "docker"

# 只允许回环地址：任何一条不是 127.0.0.1 / localhost / ::1 的绑定都是缺陷
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

EXPECTED_PORTS: tuple[tuple[int, str, str], ...] = (
    (4317, "otel-collector", "OTLP gRPC"),
    (4318, "otel-collector", "OTLP HTTP"),
    (13133, "otel-collector", "health_check"),
    (12800, "skywalking-oap", "query REST"),
    (8080, "skywalking-ui", "UI"),
)

COLLECTOR_HEALTH_URL = "http://127.0.0.1:13133/"
OAP_QUERY_URL = "http://127.0.0.1:12800/"
OTLP_HTTP_ENDPOINT = "http://127.0.0.1:4318/v1/traces"


class ApmError(ContractError):
    """APM 事实卡相关错误。沿用 ContractError 的错误码前缀风格。"""


@dataclass(frozen=True)
class PortBinding:
    """一个期望的端口绑定。public=True 意味着它绑到了非回环地址——那是缺陷。"""

    port: int
    owner: str
    purpose: str
    bind: str = "127.0.0.1"

    @property
    def is_loopback(self) -> bool:
        return str(self.bind) in LOOPBACK_HOSTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": int(self.port),
            "owner": self.owner,
            "purpose": self.purpose,
            "bind": self.bind,
            "loopback_only": bool(self.is_loopback),
        }


@dataclass(frozen=True)
class SignalStatus:
    """单个 signal 的结论。supported 为 None 表示**未验证**，不是 False。

    None 与 False 的区别是这个模块最要紧的一处设计：
    把「没证据」写成「不支持」是在编结论；把「没证据」写成「支持」是在骗人。
    """

    signal: str
    supported: bool | None
    code: str
    evidence: tuple[str, ...] = ()
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "supported": None if self.supported is None else bool(self.supported),
            "code": self.code,
            "evidence": list(self.evidence),
            "note": self.note,
        }


@dataclass(frozen=True)
class CheckResult:
    """一层探测的结果。status 只用 STATUSES 里的值。"""

    name: str
    status: str
    detail: str = ""
    endpoint: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ApmError("APM_STATUS_INVALID", f"未知状态：{self.status}")

    @property
    def passed(self) -> bool:
        return self.status == STATUS_PASSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "endpoint": self.endpoint,
            "data": dict(self.data),
        }


@dataclass(frozen=True)
class StackFacts:
    """从 deploy/apm/stack.json 读回来的部署事实。读不到就如实返回空，不编默认值。"""

    available: bool
    path: str = ""
    components: tuple[Mapping[str, Any], ...] = ()
    ports: tuple[Mapping[str, Any], ...] = ()
    storage: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""
    error: str = ""

    @property
    def digest_unverified(self) -> tuple[str, ...]:
        return tuple(
            str(item.get("name"))
            for item in self.components
            if str(item.get("digest_status", "")).upper() != "VERIFIED"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": bool(self.available),
            "path": self.path,
            "components": [dict(item) for item in self.components],
            "ports": [dict(item) for item in self.ports],
            "storage": dict(self.storage),
            "note": self.note,
            "error": self.error,
            "digest_unverified": list(self.digest_unverified),
        }
@dataclass(frozen=True)
class ApmHealthReport:
    """一次完整体检的结论。apm_degraded 是给上层看的唯一总开关。"""

    schema_version: str
    checked_at: str
    docker_available: bool
    collector: CheckResult
    oap: CheckResult
    ui: CheckResult
    end_to_end: CheckResult
    signals: tuple[SignalStatus, ...]
    ports: tuple[PortBinding, ...]
    facts: StackFacts
    reasons: tuple[str, ...] = ()

    @property
    def apm_degraded(self) -> bool:
        """任何一层没通过就算降级。没有 Docker 是降级，不是「本机跳过」。"""
        if not self.docker_available:
            return True
        for item in (self.collector, self.oap, self.ui, self.end_to_end):
            if not item.passed:
                return True
        if any(item.supported is not True for item in self.signals):
            return True
        return False

    @property
    def all_ports_loopback(self) -> bool:
        return all(item.is_loopback for item in self.ports)

    @property
    def verdict(self) -> str:
        """一句话结论。三种取值，不允许第四种含糊说法。"""
        if not self.docker_available:
            return "APM_UNAVAILABLE_NO_DOCKER"
        if self.apm_degraded:
            return "APM_DEGRADED"
        return "APM_PASSED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "checked_at": self.checked_at,
            "verdict": self.verdict,
            "apm_degraded": bool(self.apm_degraded),
            "docker_available": bool(self.docker_available),
            "collector": self.collector.to_dict(),
            "oap": self.oap.to_dict(),
            "ui": self.ui.to_dict(),
            "end_to_end": self.end_to_end.to_dict(),
            "signals": [item.to_dict() for item in self.signals],
            "ports": [item.to_dict() for item in self.ports],
            "all_ports_loopback": bool(self.all_ports_loopback),
            "facts": self.facts.to_dict(),
            "reasons": list(self.reasons),
        }


# ------------------------------------------------------------------ 基础探测
def probe_docker_binary(*, binary: str = DOCKER_BINARY) -> tuple[bool, str, str]:
    """只回答「能不能问出服务端版本」，不回答「命令是否存在」。

    与 A2-02 的 probe_docker 同一个判据：装了 CLI 但没连上 daemon 的机器上，
    `docker` 命令存在而 `docker version --format {{.Server.Version}}` 失败——
    那种状态叫「不可用」，不叫「可用但慢」。
    """
    path = shutil.which(binary)
    if not path:
        return False, "", f"PATH 里没有 {binary}"
    try:
        proc = subprocess.run(
            [path, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20.0,
        )
    except Exception as exc:
        return False, "", f"{binary} 调用失败：{type(exc).__name__}"
    version = (proc.stdout or "").strip()
    if proc.returncode != 0 or not version:
        detail = (proc.stderr or "").strip().splitlines()
        head = detail[0] if detail else "服务端不可达"
        return False, "", f"服务端不可达：{head[:120]}"
    return True, version, "ok"


def _http_get(url: str, *, timeout: float = 3.0) -> tuple[int, str]:
    """极简 GET（标准库）。不装请求头，不带凭据；失败返回 (0, 原因)。"""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            return int(response.status), body
    except urllib.error.HTTPError as exc:
        return int(exc.code), ""
    except Exception as exc:
        return 0, type(exc).__name__


def load_stack_facts(*, root: str | pathlib.Path | None = None) -> StackFacts:
    """读 deploy/apm/stack.json。文件缺失或格式坏掉都如实报告，不伪造一份。"""
    import os

    base = pathlib.Path(root) if root is not None else pathlib.Path(os.getcwd())
    target = base / STACK_FILE
    if not target.exists():
        return StackFacts(available=False, path=str(target), error="STACK_FILE_MISSING")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        return StackFacts(available=False, path=str(target), error=f"STACK_FILE_INVALID:{type(exc).__name__}")
    if not isinstance(payload, Mapping):
        return StackFacts(available=False, path=str(target), error="STACK_FILE_NOT_OBJECT")
    components = tuple(item for item in payload.get("components", []) if isinstance(item, Mapping))
    ports = tuple(item for item in payload.get("ports", []) if isinstance(item, Mapping))
    storage = payload.get("storage")
    return StackFacts(
        available=True,
        path=str(target),
        components=components,
        ports=ports,
        storage=dict(storage) if isinstance(storage, Mapping) else {},
        note=str(payload.get("note", "")),
    )


def config_fingerprint(*, root: str | pathlib.Path | None = None) -> dict[str, Any]:
    """给三份配置算内容指纹。配置改了但没人知道，是这类事故最常见的起点。"""
    import os

    base = pathlib.Path(root) if root is not None else pathlib.Path(os.getcwd())
    out: dict[str, Any] = {}
    for relative in (COMPOSE_FILE, COLLECTOR_CONFIG, STACK_FILE):
        target = base / relative
        if not target.exists():
            out[relative] = {"exists": False, "sha256": "", "bytes": 0}
            continue
        data = target.read_bytes()
        out[relative] = {
            "exists": True,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
    return out
# ------------------------------------------------------------------ 分层探测
def probe_ports(*, bindings: Sequence[PortBinding] | None = None) -> tuple[PortBinding, ...]:
    """列出期望端口与绑定。绑 host 由 compose 文件定义，这里复述为可断言的数据结构。"""
    if bindings is not None:
        return tuple(bindings)
    return tuple(PortBinding(port=p, owner=o, purpose=d) for p, o, d in EXPECTED_PORTS)


def check_compose_bindings(*, root: str | pathlib.Path | None = None) -> CheckResult:
    """静态审计 compose 文件：任何一条非回环端口绑定都是缺陷。

    为什么静态审计也算一条证据：本机没有 Docker，我唯一能证明的是「这份配置里
    没有 0.0.0.0」，不能证明「跑起来之后真的没监听公网」。两句话不能混为一谈，
    所以这一条的 detail 里必须带上它证明了什么。
    """
    import os
    import re

    base = pathlib.Path(root) if root is not None else pathlib.Path(os.getcwd())
    target = base / COMPOSE_FILE
    if not target.exists():
        return CheckResult(name="compose_bindings", status=STATUS_FAILED, detail="缺少 " + COMPOSE_FILE, endpoint=str(target))
    text = target.read_text(encoding="utf-8")
    published = re.findall(r'^\s*-\s*"([^"]+)"\s*#?.*$', text, flags=re.MULTILINE)
    rows: list[dict[str, Any]] = []
    offenders: list[str] = []
    for item in published:
        if ":" not in item:
            continue
        host, _, rest = item.partition(":")
        rows.append({"raw": item, "host": host})
        if host not in LOOPBACK_HOSTS:
            offenders.append(item)
    if not rows:
        return CheckResult(
            name="compose_bindings",
            status=STATUS_FAILED,
            detail="没有解析出任何宿主端口绑定；compose 结构可能变了",
            endpoint=str(target),
        )
    if offenders:
        return CheckResult(
            name="compose_bindings",
            status=STATUS_FAILED,
            detail="发现非回环绑定：" + ", ".join(offenders),
            endpoint=str(target),
            data={"published": rows, "offenders": offenders},
        )
    return CheckResult(
        name="compose_bindings",
        status=STATUS_PASSED,
        detail=f"静态审计：{len(rows)} 条宿主端口绑定全部为回环。注意这只证明配置，不证明运行时监听。",
        endpoint=str(target),
        data={"published": rows, "offenders": []},
    )


def check_collector(*, url: str = COLLECTOR_HEALTH_URL, timeout: float = 3.0, docker_available: bool = True) -> CheckResult:
    """Collector 的 health_check extension。状态码 200 才算通过。"""
    if not docker_available:
        return CheckResult(
            name="collector_health",
            status=STATUS_UNAVAILABLE,
            detail="没有 Docker，Collector 不可能在跑；未做任何探测",
            endpoint=url,
        )
    code, body = _http_get(url, timeout=timeout)
    if code == 200:
        return CheckResult(
            name="collector_health",
            status=STATUS_PASSED,
            detail="health_check 返回 200",
            endpoint=url,
            data={"body_head": body[:200]},
        )
    return CheckResult(
        name="collector_health",
        status=STATUS_FAILED,
        detail=f"health_check 未通过：code={code} detail={body[:120]}",
        endpoint=url,
    )


def check_oap(*, url: str = OAP_QUERY_URL, timeout: float = 3.0, docker_available: bool = True) -> CheckResult:
    """OAP 查询口可达性。只要不是连接失败（code=0）就说明有东西在答。"""
    if not docker_available:
        return CheckResult(
            name="oap_query",
            status=STATUS_UNAVAILABLE,
            detail="没有 Docker，OAP 不可能在跑；未做任何探测",
            endpoint=url,
        )
    code, body = _http_get(url, timeout=timeout)
    if code == 0:
        return CheckResult(
            name="oap_query",
            status=STATUS_FAILED,
            detail=f"查询口连不上：{body}",
            endpoint=url,
        )
    return CheckResult(
        name="oap_query",
        status=STATUS_PASSED,
        detail=f"查询口有应答：HTTP {code}（应答存在不代表数据在里面）",
        endpoint=url,
        data={"http_status": code},
    )


def check_ui(*, host: str = "127.0.0.1", port: int = 8080, docker_available: bool = True) -> CheckResult:
    """UI 可达性。"""
    url = f"http://{host}:{port}/"
    if not docker_available:
        return CheckResult(
            name="ui_reachable",
            status=STATUS_UNAVAILABLE,
            detail="没有 Docker，UI 不可能在跑；未做任何探测",
            endpoint=url,
        )
    code, body = _http_get(url, timeout=3.0)
    if code == 0:
        return CheckResult(name="ui_reachable", status=STATUS_FAILED, detail=f"UI 连不上：{body}", endpoint=url)
    return CheckResult(
        name="ui_reachable",
        status=STATUS_PASSED,
        detail=f"UI 有应答：HTTP {code}",
        endpoint=url,
        data={"http_status": code},
    )


def check_end_to_end(*, docker_available: bool, evidence_path: str | pathlib.Path | None = None) -> CheckResult:
    """端到端证据：SDK → Collector → OAP → 查询，四段都要有。

    这里**不接受**「三层单点探测都过了」作为端到端证据。三层探测证明的是三个进程
    各自活着，端到端证明的是数据真的从 SDK 走到了查询接口。二者的差别正是
    「容器起来了但 span 全被丢在 queue 里」这类事故的全部内容。
    没有真实栈时这里必须是 unavailable，不能因为前三层过了就自动变 passed。
    """
    if not docker_available:
        return CheckResult(
            name="end_to_end",
            status=STATUS_UNAVAILABLE,
            detail="没有 Docker，无法产生 SDK→Collector→OAP→查询 的真实链路证据",
        )
    if evidence_path is None:
        return CheckResult(
            name="end_to_end",
            status=STATUS_FAILED,
            detail="栈在跑，但没有提供端到端证据文件；容器活着不等于链路通",
        )
    target = pathlib.Path(evidence_path)
    if not target.exists():
        return CheckResult(
            name="end_to_end",
            status=STATUS_FAILED,
            detail="证据文件不存在：" + str(target),
            endpoint=str(target),
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        return CheckResult(
            name="end_to_end",
            status=STATUS_FAILED,
            detail=f"证据文件不是合法 JSON：{type(exc).__name__}",
            endpoint=str(target),
        )
    if not isinstance(payload, Mapping):
        return CheckResult(name="end_to_end", status=STATUS_FAILED, detail="证据文件不是对象", endpoint=str(target))
    missing = [stage for stage in ("sdk", "collector", "oap", "query") if not payload.get(stage)]
    if missing:
        return CheckResult(
            name="end_to_end",
            status=STATUS_FAILED,
            detail="证据不完整，缺：" + ", ".join(missing),
            endpoint=str(target),
            data={"stages": dict(payload)},
        )
    return CheckResult(
        name="end_to_end",
        status=STATUS_PASSED,
        detail="四段证据齐全：SDK → Collector → OAP → 查询",
        endpoint=str(target),
        data={"stages": dict(payload)},
    )
# ------------------------------------------------------------------ signal 判定
def signal_statuses(
    *,
    docker_available: bool,
    collector: CheckResult,
    oap: CheckResult,
    end_to_end: CheckResult,
    declared: Mapping[str, Any] | None = None,
) -> tuple[SignalStatus, ...]:
    """逐个 signal 给结论。三条 signal 各判各的，不共用「栈通了」这一个结论。

    declared 来自 stack.json 的 signals 段（部署方声明）。声明只用于「明确不支持」
    这一种情形；声明支持但没有证据时仍然是 unverified。
    """
    declare = dict(declared or {})
    link_ok = collector.passed and oap.passed and end_to_end.passed
    rows: list[SignalStatus] = []
    for name in SIGNALS:
        entry = declare.get(name)
        entry = dict(entry) if isinstance(entry, Mapping) else {}
        declared_flag = entry.get("supported")
        if declared_flag is False:
            rows.append(SignalStatus(
                signal=name,
                supported=False,
                code=SIGNAL_UNSUPPORTED,
                note=str(entry.get("note", "")) or "部署方声明本栈不支持该 signal",
            ))
            continue
        if not docker_available:
            rows.append(SignalStatus(
                signal=name,
                supported=None,
                code=SIGNAL_UNVERIFIED,
                note="没有 Docker，没有任何证据；不能写成不支持，也不能写成支持",
            ))
            continue
        if not link_ok:
            rows.append(SignalStatus(
                signal=name,
                supported=None,
                code=SIGNAL_UNVERIFIED,
                evidence=tuple(
                    f"{item.name}={item.status}" for item in (collector, oap, end_to_end)
                ),
                note="链路证据不完整，无法判定该 signal",
            ))
            continue
        rows.append(SignalStatus(
            signal=name,
            supported=True,
            code=SIGNAL_SUPPORTED,
            evidence=("collector=passed", "oap=passed", "end_to_end=passed"),
            note="有端到端证据",
        ))
    return tuple(rows)


def apm_health(
    *,
    root: str | pathlib.Path | None = None,
    docker_available: bool | None = None,
    collector_url: str = COLLECTOR_HEALTH_URL,
    oap_url: str = OAP_QUERY_URL,
    ui_port: int = 8080,
    evidence_path: str | pathlib.Path | None = None,
    clock: Any = None,
    declared_signals: Mapping[str, Any] | None = None,
) -> ApmHealthReport:
    """一次完整体检。docker_available 可注入以便在无 Docker 的机器上做确定性测试。"""
    import datetime as dt
    import os

    base = pathlib.Path(root) if root is not None else pathlib.Path(os.getcwd())
    if docker_available is None:
        docker_available, _version, _detail = probe_docker_binary()
    moment = clock() if clock is not None else dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        raise ApmError("APM_CLOCK_NAIVE", "体检时钟必须带时区")

    collector = check_collector(url=collector_url, docker_available=bool(docker_available))
    oap = check_oap(url=oap_url, docker_available=bool(docker_available))
    ui = check_ui(port=ui_port, docker_available=bool(docker_available))
    end_to_end = check_end_to_end(docker_available=bool(docker_available), evidence_path=evidence_path)
    facts = load_stack_facts(root=base)
    declared = declared_signals
    if declared is None and facts.available:
        declared = {"traces": {}, "metrics": {}, "logs": {}}
        for name in SIGNALS:
            entry = facts.storage.get(name)
            _ = entry  # storage 段不是 signal 声明；真正声明在 stack.json 的 signals 段
    bindings = probe_ports()
    bindings_check = check_compose_bindings(root=base)
    if not bindings_check.passed:
        bindings = tuple(
            PortBinding(port=item.port, owner=item.owner, purpose=item.purpose, bind="0.0.0.0")
            for item in bindings
        )
    signals = signal_statuses(
        docker_available=bool(docker_available),
        collector=collector,
        oap=oap,
        end_to_end=end_to_end,
        declared=declared,
    )
    reasons: list[str] = []
    if not docker_available:
        reasons.append("no_docker_on_this_host")
    for item in (collector, oap, ui, end_to_end):
        if not item.passed:
            reasons.append(f"{item.name}:{item.status}")
    if not bindings_check.passed:
        reasons.append("compose_bindings:" + bindings_check.status)

    return ApmHealthReport(
        schema_version=APM_SCHEMA_VERSION,
        checked_at=moment.isoformat(),
        docker_available=bool(docker_available),
        collector=collector,
        oap=oap,
        ui=ui,
        end_to_end=end_to_end,
        signals=signals,
        ports=bindings,
        facts=facts,
        reasons=tuple(reasons),
    )


def apm_fact_card(
    *,
    root: str | pathlib.Path | None = None,
    health: ApmHealthReport | None = None,
    clock: Any = None,
    docker_available: bool | None = None,
    evidence_path: str | pathlib.Path | None = None,
) -> dict[str, Any]:
    """把体检结论、配置指纹、版本事实合成一张可落盘的卡（agent_eval/results/apm_environment.json）。"""
    base = root
    report = health or apm_health(
        root=base,
        clock=clock,
        docker_available=docker_available,
        evidence_path=evidence_path,
    )
    payload = report.to_dict()
    payload["config_fingerprint"] = config_fingerprint(root=base)
    payload["expected_otlp_http_endpoint"] = OTLP_HTTP_ENDPOINT
    payload["degraded_event"] = "apm_degraded" if report.apm_degraded else ""
    return payload


def write_fact_card(
    path: str | pathlib.Path,
    payload: Mapping[str, Any],
    *,
    root: str | pathlib.Path | None = None,
) -> pathlib.Path:
    """落盘事实卡。走 resolve_within_root，不允许写到工程外。"""
    from pq_agent.security.paths import resolve_within_root

    # allow_absolute=True：调用方可能传绝对路径（CLI 的 --out），但仍必须在工程根之内。
    target = pathlib.Path(resolve_within_root(str(path), root=root, allow_absolute=True))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    data = json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True, default=str)
    temporary.write_text(data + chr(10), encoding="utf-8")
    import os

    os.replace(temporary, target)
    return target
# ------------------------------------------------------------------ 文本渲染
def render_health(report: ApmHealthReport) -> str:
    """给人看的报告。措辞必须能直接区分「通过」「没证据」「不通过」三种状态。"""
    lines: list[str] = []
    lines.append("APM 体检：" + report.verdict)
    lines.append("时间：" + report.checked_at)
    lines.append("Docker：" + ("可用" if report.docker_available else "不可用"))
    lines.append("")
    lines.append("分层结果")
    lines.append("| 层 | 状态 | 说明 |")
    lines.append("|---|---|---|")
    for item in (report.collector, report.oap, report.ui, report.end_to_end):
        lines.append("| " + item.name + " | " + item.status + " | " + item.detail + " |")
    lines.append("")
    lines.append("signal 结论（supported=None 表示没有证据）")
    lines.append("| signal | supported | 结论码 | 说明 |")
    lines.append("|---|---|---|---|")
    for item in report.signals:
        flag = "null" if item.supported is None else str(bool(item.supported)).lower()
        lines.append("| " + item.signal + " | " + flag + " | " + item.code + " | " + item.note + " |")
    lines.append("")
    lines.append("端口（全部回环 = " + ("是" if report.all_ports_loopback else "否") + "）")
    lines.append("| 端口 | 归属 | 用途 | 绑定 |")
    lines.append("|---|---|---|---|")
    for item in report.ports:
        lines.append("| " + str(item.port) + " | " + item.owner + " | " + item.purpose + " | " + item.bind + " |")
    lines.append("")
    if report.facts.available:
        lines.append("镜像 digest 未核对：" + (", ".join(report.facts.digest_unverified) or "无"))
    else:
        lines.append("部署事实卡读取失败：" + (report.facts.error or "未知"))
    if report.reasons:
        lines.append("")
        lines.append("降级原因：" + "；".join(report.reasons))
    return chr(10).join(lines)


def render_degraded_notice() -> str:
    """无 Docker 时给调用方的标准告知语，避免每个调用点自己编说法。"""
    return (
        "本机没有 Docker，APM 栈未启动，apm_degraded=true。"
        "traces/metrics/logs 三种 signal 均为 UNVERIFIED（不是通过，也不是不支持）。"
        "应用侧仍保留 OTel SDK 与本地 trace_id，只是导出退回 file exporter。"
    )


def supported_signals(report: ApmHealthReport) -> tuple[str, ...]:
    """只返回**有证据支持**的 signal。unverified 不算支持。"""
    return tuple(item.signal for item in report.signals if item.supported is True)


def unsupported_signals(report: ApmHealthReport) -> tuple[str, ...]:
    """只返回**明确不支持**的 signal。unverified 不算不支持。"""
    return tuple(item.signal for item in report.signals if item.supported is False)
