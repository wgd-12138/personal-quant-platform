"""A1-06 本地降级事件日志：没有 Collector 时的可观测底线。

企业里的真实取舍：可观测后端挂了，业务不能跟着挂，但**也不能装作什么都没发生**。
所以这里做三件事：

1. 把"观测降级"本身记成一条事件（而不是静默失败）；
2. 事件落本地 JSONL，带大小上限与轮转，避免把磁盘写满；
3. 每条事件出厂前过脱敏闸门，宁可丢事件也不写出未脱敏内容。

为什么不用 logging 模块：事件需要结构化字段（event / detail / count）供评测与断言，
且必须能被"程序化扫描秘密命中数"这一步直接读取。
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pq_agent.contracts import ContractError
from pq_agent.security.redaction import guard_payload

EVENT_LOG_SCHEMA_VERSION = 1

ERROR_EVENT_LOG_IO = "TELEMETRY_EVENT_LOG_IO_FAILED"
ERROR_EVENT_LOG_INVALID = "TELEMETRY_EVENT_INVALID"

DEFAULT_MAX_BYTES = 1_000_000

# 允许的事件名。未知事件名直接拒绝——防止有人把任意文本当事件写进去。
EVENT_NAMES: tuple[str, ...] = (
    "tracing_degraded",
    "exporter_unavailable",
    "attribute_dropped",
    "span_error",
    "usage_unknown",
    "shutdown",
    # A4 新增：APM 栈相关事件。必须在这里登记，否则 record() 抛 TELEMETRY_EVENT_INVALID，
    # 而「观测层自己报错」是最难查的一类故障——它安静地把证据吃掉了。
    "apm_degraded",
    "collector_dropped",
)


def _now_iso(moment: dt.datetime | None) -> str:
    stamp = moment if moment is not None else dt.datetime.now(dt.timezone.utc)
    if stamp.tzinfo is None:
        raise ContractError("DATETIME_NAIVE", "事件时间必须带时区")
    return stamp.isoformat()


class LocalEventLog:
    """限权的本地事件日志：超限先轮转，再失败也不影响主流程（由调用方决定）。"""

    def __init__(
        self,
        path: str | pathlib.Path | None = None,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        secrets: Iterable[str] | None = None,
        clock=None,
    ) -> None:
        self.path = pathlib.Path(path) if path is not None else None
        if max_bytes <= 0:
            raise ContractError("TELEMETRY_LOG_LIMIT_INVALID", f"max_bytes={max_bytes}")
        self.max_bytes = int(max_bytes)
        self._secrets = tuple(s for s in (secrets or ()) if isinstance(s, str) and s.strip())
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._events: list[dict[str, Any]] = []
        self._dropped = 0
        self.rotations = 0

    # ---- 读属性 ----------------------------------------------------------
    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    @property
    def dropped(self) -> int:
        return int(self._dropped)

    def count(self, event: str) -> int:
        return sum(1 for item in self._events if item.get("event") == event)

    def read_events(self) -> list[dict[str, Any]]:
        """把已经落盘的事件读回来（供"秘密命中数=0"的程序化扫描使用）。"""
        if self.path is None or not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                rows.append(json.loads(text))
            except Exception:
                rows.append({"event": "unparsable", "raw": text})
        return rows

    # ---- 写入 ------------------------------------------------------------
    def record(self, event: str, *, detail: str = "", **fields: Any) -> dict[str, Any]:
        """记一条事件。返回落盘的那条（已脱敏），便于调用方断言。"""
        if event not in EVENT_NAMES:
            raise ContractError(ERROR_EVENT_LOG_INVALID, f"未知事件名：{event}")
        payload: dict[str, Any] = {
            "schema_version": EVENT_LOG_SCHEMA_VERSION,
            "event": event,
            "at": _now_iso(self._clock()),
            "detail": str(detail)[:500],
        }
        for key, value in fields.items():
            text = str(key)
            if text in payload:
                continue
            payload[text] = value
        try:
            cleaned = guard_payload(payload, secrets=self._secrets, where="telemetry.event_log")
        except Exception as exc:
            # 脱敏失败：事件丢弃并计数，绝不把原文写出去
            self._dropped += 1
            raise ContractError(
                ERROR_EVENT_LOG_INVALID,
                f"事件 {event} 未通过脱敏闸门：{type(exc).__name__}",
            ) from exc
        self._events.append(cleaned)
        self._persist(cleaned)
        return cleaned

    def _persist(self, payload: Mapping[str, Any]) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                self._rotate()
            with self.path.open("a", encoding="utf-8", newline=chr(10)) as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                handle.write(chr(10))
        except Exception:
            # 落盘失败只计数，不让观测层拖垮主流程
            self._dropped += 1

    def _rotate(self) -> None:
        assert self.path is not None
        index = 1
        while True:
            candidate = self.path.with_name(self.path.name + f".{index}")
            if not candidate.exists():
                break
            index += 1
        self.path.rename(candidate)
        self.rotations += 1


@dataclass
class TracingStatus:
    """观测状态。降级原因必须能被读出来，不能只写日志。"""

    enabled: bool = False
    exporter: str = "none"
    endpoint: str = ""
    degraded: bool = False,
    reasons: list[str] = field(default_factory=list)
    span_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "exporter": self.exporter,
            "endpoint": self.endpoint,
            "degraded": bool(self.degraded),
            "reasons": list(self.reasons),
            "span_count": int(self.span_count),
        }

    def note(self, reason: str) -> None:
        text = str(reason)
        if text and text not in self.reasons:
            self.reasons.append(text)
        self.degraded = True


@dataclass
class TracingState:
    """已经装配好的跟踪环境：provider / tracer / sampler / 事件日志 / 状态。"""

    tracer: Any
    provider: Any
    sampler: Any
    event_log: LocalEventLog
    status: TracingStatus
    redactor: Any = None

    def status_dict(self) -> dict[str, Any]:
        return self.status.to_dict()


SERVICE_TRACER_NAME = "pq_agent.observability"


def _parallel_sampler(ratio: float):
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    if ratio >= 1.0:
        return None
    return ParentBased(root=TraceIdRatioBased(float(ratio)))


def build_tracer(
    *,
    resource_config=None,
    exporter: Any = None,
    exporter_kind: str = "none",
    sample_ratio: float = 1.0,
    event_log: LocalEventLog | None = None,
    secrets: Iterable[str] | None = None,
    redactor: Any = None,
    endpoint: str = "",
    shutdown_on_exit: bool = False,
):
    """装配 OTel tracer。**这是唯一构造 provider 的地方**，换后端只换 exporter。

    exporter=None 时不做任何导出，并显式标记观测降级（没有 Collector 是事实，不是故障）。
    """
    from opentelemetry.sdk.trace import TracerProvider

    from .redaction import TelemetryRedactor
    from .resource import ResourceConfig, build_resource, detect_commit, detect_environment, detect_version

    config = resource_config
    if config is None:
        config = ResourceConfig(
            environment=detect_environment(),
            commit=detect_commit(),
            version=detect_version(),
        )
    secret_tuple = tuple(s for s in (secrets or ()) if isinstance(s, str) and s.strip())
    active_redactor = redactor or TelemetryRedactor(secrets=secret_tuple)
    log = event_log if event_log is not None else LocalEventLog(secrets=secret_tuple)
    status = TracingStatus(
        enabled=True,
        exporter=str(exporter_kind),
        endpoint=str(endpoint or ""),
        degraded=False,
        reasons=[],
        span_count=0,
    )

    sampler = _parallel_sampler(sample_ratio)
    provider = TracerProvider(
        resource=build_resource(config),
        sampler=sampler,
        shutdown_on_exit=bool(shutdown_on_exit),
    )
    if exporter is None:
        status.note("no_exporter_configured")
        log.record("tracing_degraded", detail="未配置 exporter，span 只在本进程生成，不对外导出")
    # 出口包装：无论真 OTLP 还是本地扇出，属性都再过一遍脱敏闸门
    exporter = wrap_exporter(exporter, secrets=secret_tuple, event_log=log, status=status)
    if exporter is not None:
        provider.add_span_processor(_build_batch_processor(exporter))

    tracer = provider.get_tracer(SERVICE_TRACER_NAME)
    state = TracingState(
        tracer=tracer,
        provider=provider,
        sampler=sampler,
        event_log=log,
        status=status,
        redactor=active_redactor,
    )
    return state


def wrap_exporter(exporter: Any, *, secrets=None, event_log: "LocalEventLog | None" = None, status: "TracingStatus | None" = None):
    """给 exporter 套上脱敏出口。exporter 为 None 时原样返回。

    构造失败不抛异常：观测层不能拖垮主流程，但必须记一条降级事件。
    """
    if exporter is None:
        return None
    from .redaction import SanitizingSpanExporter

    try:
        return SanitizingSpanExporter(exporter, secrets=secrets)
    except Exception as exc:
        if event_log is not None:
            try:
                event_log.record("exporter_unavailable", detail=f"出口脱敏包装失败：{type(exc).__name__}")
            except Exception:
                pass
        if status is not None:
            status.note("sanitizing_exporter_failed")
        return None

def _build_batch_processor(exporter: Any):
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    return BatchSpanProcessor(exporter)


def build_otlp_exporter(
    *,
    endpoint: str | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_s: float = 5.0,
    event_log: LocalEventLog | None = None,
):
    """构造 OTLP HTTP exporter。缺包或构造失败时返回 None 并记降级事件。"""
    import os

    target = endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or ""
    log = event_log or LocalEventLog()
    if not target:
        log.record("exporter_unavailable", detail="未提供 OTLP endpoint，观测降级为本地模式")
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=target, headers=dict(headers or {}), timeout=float(timeout_s))
    except Exception as exc:
        log.record("exporter_unavailable", detail=f"OTLP exporter 构造失败：{type(exc).__name__}")
        return None


_CURRENT: list[TracingState | None] = [None]


def configure_tracing(
    *,
    exporter: Any = None,
    exporter_kind: str = "none",
    sample_ratio: float = 1.0,
    event_log: LocalEventLog | None = None,
    secrets: Iterable[str] | None = None,
    endpoint: str = "",
    resource_config=None,
) -> TracingState:
    """进程级装配。重复调用会先关掉旧的 provider，避免 exporter 泄漏。"""
    existing = _CURRENT[0]
    if existing is not None:
        shutdown_tracing()
    state = build_tracer(
        resource_config=resource_config,
        exporter=exporter,
        exporter_kind=exporter_kind,
        sample_ratio=sample_ratio,
        event_log=event_log,
        secrets=secrets,
        endpoint=endpoint,
    )
    _CURRENT[0] = state
    return state


def current_tracing() -> TracingState | None:
    return _CURRENT[0]


def get_tracer(default: Any = None) -> Any:
    """取当前 tracer；未装配时返回 default（通常是 None），不自动创建一个假的。"""
    state = _CURRENT[0]
    if state is None:
        return default
    return state.tracer


def shutdown_tracing() -> None:
    state = _CURRENT[0]
    _CURRENT[0] = None
    if state is None:
        return
    try:
        state.provider.shutdown()
    except Exception:
        pass
    try:
        state.event_log.record("shutdown", detail="tracer provider 已关闭")
    except Exception:
        pass


def clear_tracing() -> None:
    """单测用：只清当前指针，不触发 provider.shutdown。"""
    _CURRENT[0] = None
