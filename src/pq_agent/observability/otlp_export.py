"""A6-05：把本项目的 span / metric / log 真的通过 OTLP 发出去。

为什么单独一个模块（而不是塞进 graph.py）：
1. 三信号要能分别关掉：测试里只想验 traces 时，不该顺带发 metrics。
2. exporter 只能是「显式传入」，绝不读环境变量偷偷连远端，
   线上最怕的就是「本地测试跑着跑着把数据发到了生产 Collector」。
3. Collector 不在时不能把主流程拖死：批量导出器在后台线程里失败，
   主流程只记一条 degraded 事件。

设计取舍：
- metrics 和 logs 用真实 OTel SDK 的 MeterProvider / LoggerProvider，
  不用自己拼 protobuf：自己拼的东西没法被 Collector 校验。
- 本地 TraceRecordingExporter 只在显式 offline 模式下使用，
  这个模块不提供任何「自动降级成本地记录」的分支。
"""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Iterable, Mapping
from typing import Any

from pq_agent.observability.event_log import LocalEventLog

SIGNAL_TRACES = "traces"
SIGNAL_METRICS = "metrics"
SIGNAL_LOGS = "logs"
OTLP_SIGNALS = (SIGNAL_TRACES, SIGNAL_METRICS, SIGNAL_LOGS)

OTLP_SCHEMA_VERSION = "pq_agent.otlp/1"


def normalize_endpoint(endpoint: str) -> str:
    """把 base endpoint 规范成不带尾部斜杠的形式。空值直接失败。"""
    text = str(endpoint or "").strip()
    if not text:
        raise ValueError("OTLP_ENDPOINT_REQUIRED: 必须显式给出 Collector 地址")
    if not (text.startswith("http://") or text.startswith("https://")):
        raise ValueError("OTLP_ENDPOINT_SCHEME_INVALID: 只支持 http/https，收到：" + text)
    return text.rstrip("/")


def trace_endpoint(endpoint: str) -> str:
    return normalize_endpoint(endpoint) + "/v1/traces"


def metric_endpoint(endpoint: str) -> str:
    return normalize_endpoint(endpoint) + "/v1/metrics"


def log_endpoint(endpoint: str) -> str:
    return normalize_endpoint(endpoint) + "/v1/logs"


def build_signal_exporters(
    *,
    endpoint: str,
    event_log: LocalEventLog | None = None,
    headers: Mapping[str, str] | None = None,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """构造三信号 exporter。缺哪个 SDK 包就把哪个标成 None 并记一条降级事件。"""
    log = event_log or LocalEventLog()
    target = normalize_endpoint(endpoint)
    out: dict[str, Any] = {SIGNAL_TRACES: None, SIGNAL_METRICS: None, SIGNAL_LOGS: None}
    reasons: dict[str, str] = {}

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        out[SIGNAL_TRACES] = OTLPSpanExporter(
            endpoint=trace_endpoint(target), headers=dict(headers or {}), timeout=float(timeout_s)
        )
    except Exception as exc:  # noqa: BLE001
        reasons[SIGNAL_TRACES] = type(exc).__name__ + ": " + str(exc)[:120]

    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

        out[SIGNAL_METRICS] = OTLPMetricExporter(
            endpoint=metric_endpoint(target), headers=dict(headers or {}), timeout=float(timeout_s)
        )
    except Exception as exc:  # noqa: BLE001
        reasons[SIGNAL_METRICS] = type(exc).__name__ + ": " + str(exc)[:120]

    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

        out[SIGNAL_LOGS] = OTLPLogExporter(
            endpoint=log_endpoint(target), headers=dict(headers or {}), timeout=float(timeout_s)
        )
    except Exception as exc:  # noqa: BLE001
        reasons[SIGNAL_LOGS] = type(exc).__name__ + ": " + str(exc)[:120]

    if reasons:
        log.record("exporter_unavailable", detail="部分信号 exporter 构造失败：" + ";".join(sorted(reasons)))
    return out


class MetricSignalExporter:
    """把 MetricBatch 翻译成 OTel metrics 并导出。

    kind=counter 走 Counter，kind=gauge 走 ObservableGauge，kind=histogram 走 Histogram。
    翻译完立刻 force_flush：这笔数据来自一次已经结束的运行，不该等定期导出周期。
    """

    def __init__(self, *, exporter: Any, resource: Any = None) -> None:
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        self.exporter = exporter
        self._reader = PeriodicExportingMetricReader(
            exporter, export_interval_millis=600000, export_timeout_millis=10000
        )
        self._provider = MeterProvider(metric_readers=[self._reader], resource=resource)
        self._meter = self._provider.get_meter("pq_agent.observability")
        self._instruments: dict[tuple[str, str], Any] = {}
        self._gauges: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    @property
    def provider(self) -> Any:
        return self._provider

    def _instrument(self, name: str, kind: str) -> Any:
        key = (name, kind)
        with self._lock:
            if key not in self._instruments:
                if kind == "histogram":
                    self._instruments[key] = self._meter.create_histogram(name)
                else:
                    self._instruments[key] = self._meter.create_counter(name)
            return self._instruments[key]

    def export(self, batch: Any) -> dict[str, Any]:
        exported = 0
        gauges = 0
        for point in getattr(batch, "points", ()) or ():
            labels = {str(k): str(v) for k, v in dict(getattr(point, "labels", {}) or {}).items()}
            name = str(getattr(point, "name", ""))
            value = float(getattr(point, "value", 0.0))
            kind = str(getattr(point, "kind", "counter"))
            if kind == "gauge":
                holder = self._gauges.get(name)
                if holder is None:
                    holder = {"value": value, "labels": dict(labels)}
                    self._meter.create_observable_gauge(name, callbacks=[_gauge_reader(holder)])
                    self._gauges[name] = holder
                else:
                    holder["value"] = value
                    holder["labels"] = dict(labels)
                gauges += 1
                continue
            instrument = self._instrument(name, kind)
            if kind == "histogram":
                instrument.record(value, labels)
            else:
                instrument.add(value, labels)
            exported += 1
        self._provider.force_flush()
        return {"exported": exported, "gauges": gauges}

    def shutdown(self) -> None:
        try:
            self._provider.shutdown()
        except Exception:
            pass


def _gauge_reader(holder: dict[str, Any]):
    """把可变 holder 包成 observable 回调：不闭包抓值，闭包抓的是同一份 holder。"""
    from opentelemetry.metrics import Observation

    def _read(options: Any):
        return [Observation(holder["value"], dict(holder["labels"]))]

    return _read


class LogSignalExporter:
    """把 LogRecord 翻译成 OTel logs 并导出。trace_id / span_id 必须原样带过去。"""

    def __init__(self, *, exporter: Any, resource: Any = None) -> None:
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        self.exporter = exporter
        self._provider = LoggerProvider(resource=resource)
        self._provider.add_log_record_processor(
            BatchLogRecordProcessor(exporter, schedule_delay_millis=500)
        )
        self._logger = self._provider.get_logger("pq_agent.observability")

    @property
    def provider(self) -> Any:
        return self._provider

    def export(self, records: Iterable[Any]) -> dict[str, int]:
        exported = 0
        for record in records:
            body = str(getattr(record, "message", "") or getattr(record, "event", ""))
            severity = str(getattr(record, "severity", "info") or "info")
            attributes = dict(getattr(record, "fields", {}) or {})
            attributes["pq_agent.event"] = str(getattr(record, "event", ""))
            context = _log_context(str(getattr(record, "trace_id", "") or ""), str(getattr(record, "span_id", "") or ""))
            self._logger.emit(_log_record_data(body=body, severity=severity, attributes=attributes, context=context))
            exported += 1
        self._provider.force_flush()
        return {"exported": exported}

    def shutdown(self) -> None:
        try:
            self._provider.shutdown()
        except Exception:
            pass


def _log_context(trace_id: str, span_id: str) -> Any:
    """把十六进制 trace/span id 还原成日志上下文；不合法就返回 None（不带 id）。"""
    if len(trace_id) != 32 or (span_id and len(span_id) != 16):
        return None
    try:
        from opentelemetry.trace import SpanContext, TraceFlags, set_span_in_context
        from opentelemetry.trace.span import NonRecordingSpan

        ctx = SpanContext(
            trace_id=int(trace_id, 16),
            span_id=int(span_id, 16) if span_id else 0,
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        return set_span_in_context(NonRecordingSpan(ctx))
    except Exception:
        return None


def _log_record_data(*, body: str, severity: str, attributes: Mapping[str, Any], context: Any = None) -> Any:
    """构造 OTel LogRecord。级别文字与数字一起给，Collector 两边都能看。"""
    from opentelemetry._logs import LogRecord as SdkLogRecord
    from opentelemetry._logs import SeverityNumber

    table = {
        "debug": SeverityNumber.DEBUG,
        "info": SeverityNumber.INFO,
        "warn": SeverityNumber.WARN,
        "warning": SeverityNumber.WARN,
        "error": SeverityNumber.ERROR,
    }
    level = table.get(str(severity).lower(), SeverityNumber.INFO)
    return SdkLogRecord(
        timestamp=None,
        observed_timestamp=None,
        severity_text=str(severity).upper(),
        severity_number=level,
        body=body,
        attributes=dict(attributes),
        context=context,
    )


__all__ = [
    "LogSignalExporter",
    "MetricSignalExporter",
    "OTLP_SCHEMA_VERSION",
    "OTLP_SIGNALS",
    "SIGNAL_LOGS",
    "SIGNAL_METRICS",
    "SIGNAL_TRACES",
    "build_signal_exporters",
    "log_endpoint",
    "metric_endpoint",
    "normalize_endpoint",
    "trace_endpoint",
]