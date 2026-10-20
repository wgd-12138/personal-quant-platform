"""A4-02：把 OTel span 收成本地 trace 明细（拓扑 / 瀑布 / 日志关联 / 指标 / 告警 / 恢复链接）。

为什么需要这一层：

1. 本机没有 Collector，span 生成完就地丢了。可是「一次运行的调用树长什么样」
   恰恰是面试里最需要当场展示的东西，所以必须在本进程里留一份。
2. SkyWalking 的 UI 要等真机上 Docker，而 trace 层级、日志关联、告警判定这些逻辑
   不该等到那天才第一次被执行——先在本机把它们跑通，上云只是换 exporter。
3. 记录的是**真 span**：id、父子关系、起止时间全部来自 OTel，不是我们自己编的。
   这里唯一自己造的东西是「按 thread_id 归属到某次 run」，那条属性也是真 span 上带的。

诚实边界：本模块只能证明「span 生成、层级、脱敏、关联、告警判定是对的」。
它不能证明「SkyWalking UI 看板正常」——那需要在装了 Docker 的机器上跑。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pq_agent.contracts import ContractError

from pq_agent.observability.apm_alerts import (
    ALERT_RULES,
    AlertEvaluation,
    evaluate_alerts,
)
from pq_agent.observability.apm_logs import (
    LogRecord,
    build_log_record,
    correlate_logs,
    LOG_EVENT_LLM,
)
from pq_agent.observability.apm_metrics import MetricBatch, USAGE_STATUS_UNKNOWN
from pq_agent.observability.apm_signals import (
    SPAN_APPROVAL,
    SPAN_LLM,
    SPAN_NODE,
    SPAN_RAG,
    SPAN_RUN,
    SPAN_SANDBOX,
    SPAN_TOOL,
    LinkRecord,
    SpanRecord,
    TraceShape,
    is_known_span,
    trace_shape,
)

TRACE_DETAIL_SCHEMA_VERSION = "pq_agent.apm_trace_detail/1"
ERROR_TRACE_RECORD = "APM_TRACE_RECORD_INVALID"
MAX_RECORDED_SPANS = 5000

# span 名到日志事件的映射。每一个都必须是 apm_logs.LOG_EVENTS 里的名字，
# 否则 build_log_record 会当场抛错——这样「映射表和事件表脱节」不会静默通过。
SPAN_TO_LOG_EVENT: dict[str, str] = {
    SPAN_RUN: "run.end",
    SPAN_NODE: "node.end",
    SPAN_TOOL: "tool.invoke",
    SPAN_APPROVAL: "approval.decision",
    SPAN_SANDBOX: "sandbox.execute",
    SPAN_RAG: "rag.retrieve",
    # 这里必须用日志事件名，不是 span 名：apm_logs 的事件白名单里没有
    # "llm.intent" 这个 span 名，写成 span 名会在建日志时抛
    # APM_LOG_FIELD_UNKNOWN: 未登记的事件名。
    SPAN_LLM: LOG_EVENT_LLM,
}


def _hex_id(value: Any, width: int) -> str:
    """把 OTel 的整数 id 打成十六进制串。0 表示「没有」，一律给空串。"""
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return format(number, "0" + str(int(width)) + "x")


def _span_to_dict(span: Any) -> dict[str, Any]:
    """把 OTel ReadableSpan 压成可落盘、可断言的普通 dict。

    只保留标量属性：列表、字典、对象一律转成字符串。
    这一层不做脱敏（SanitizingSpanExporter 已经在上游做过了），只做类型归一。
    """
    context = getattr(span, "context", None)
    parent = getattr(span, "parent", None)
    status_code = getattr(getattr(span, "status", None), "status_code", None)
    attributes: dict[str, Any] = {}
    for key, value in dict(getattr(span, "attributes", {}) or {}).items():
        if isinstance(value, (str, int, float, bool)):
            attributes[str(key)] = value
        else:
            attributes[str(key)] = str(value)
    start_ns = int(getattr(span, "start_time", 0) or 0)
    end_ns = int(getattr(span, "end_time", 0) or 0)
    return {
        "name": str(getattr(span, "name", "")),
        "trace_id": _hex_id(getattr(context, "trace_id", 0), 32),
        "span_id": _hex_id(getattr(context, "span_id", 0), 16),
        "parent_span_id": _hex_id(getattr(parent, "span_id", 0), 16) if parent is not None else "",
        "start_ns": start_ns,
        "end_ns": end_ns,
        "status": "error" if str(status_code).endswith("ERROR") else "ok",
        "error_code": str(attributes.get("error_code") or ""),
        "attributes": attributes,
    }


class TraceRecordingExporter:
    """把 span 收进内存。它是 OTel 的 SpanExporter，所以三个方法都要实现。

    为什么不用 tests 里的 SpinCallbackExporter：那个是测试替身，只存对象；
    这里要的是可落盘、可跨进程断言的普通字典。
    """

    def __init__(self, *, max_spans: int = MAX_RECORDED_SPANS) -> None:
        self.spans: list[dict[str, Any]] = []
        self.dropped = 0
        self.shutdown_called = False
        self._max = max(1, int(max_spans))

    def export(self, spans: Sequence[Any]) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        for span in spans:
            try:
                self.spans.append(_span_to_dict(span))
            except Exception:
                # 单个 span 转不出来不影响其余：观测层不许把主流程带崩。
                self.dropped += 1
        if len(self.spans) > self._max:
            self.spans = self.spans[-self._max :]
        return SpanExportResult.SUCCESS

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def shutdown(self) -> None:
        self.shutdown_called = True

    def for_thread(self, thread_id: str, *, trace_id: str = "") -> list[dict[str, Any]]:
        """取某次运行的 span。"""

        """给了 trace_id 就按它取：OTel 的 trace_id 一次运行一个，本身就是运行身份，
        而且它写在每一个 span 上。

        只按 thread_id 属性筛会漏 span——工具 / 沙箱 / 审批 / 检索这些子 span 是
        由 apm_spans 的助手建的，它们只带自己的业务属性，不带 thread_id。
        漏掉的表现是「工具调用在拓扑里凭空消失」，而不是报错，所以这里按 trace_id 优先。

        没给 trace_id 时才退回按属性归属，用于「这个会话跑过哪条 trace」这类查询，
        那种场景下属性缺失的 span 本来就不该被认领。"""
        tid = str(thread_id or "")
        wanted = str(trace_id or "")
        if wanted:
            return [
                item
                for item in self.spans
                if str(item.get("trace_id") or "") == wanted
            ]
        if not tid:
            return []
        return [
            item
            for item in self.spans
            if str(dict(item.get("attributes") or {}).get("thread_id") or "") == tid
        ]

    def latest_trace_id(self, thread_id: str) -> str:
        rows = self.for_thread(thread_id)
        return str(rows[-1].get("trace_id") or "") if rows else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "recorded_spans": len(self.spans),
            "dropped": int(self.dropped),
            "shutdown_called": bool(self.shutdown_called),
            "max_spans": int(self._max),
        }


def _to_span_record(item: Mapping[str, Any]) -> SpanRecord | None:
    """把记录下来的 span dict 转成契约里的 SpanRecord。名字不在白名单的返回 None。

    为什么要过滤而不是原样收：OTel 自动埋点（FastAPI / httpx）会产生大量
    不在我们层级契约里的 span。把它们硬塞进 SpanRecord 会让 trace_shape 抛错，
    把它们悄悄丢掉又会让人以为「这次运行只有这些 span」——所以既不塞也不丢，
    由调用方把数量单独报出来（unmapped_spans）。
    """
    name = str(item.get("name") or "")
    if not is_known_span(name):
        return None
    start_ns = int(item.get("start_ns") or 0)
    end_ns = int(item.get("end_ns") or 0)
    duration_ms = 0.0
    if end_ns > start_ns > 0:
        duration_ms = round((end_ns - start_ns) / 1_000_000.0, 3)
    attributes = dict(item.get("attributes") or {})
    return SpanRecord(
        span_id=str(item.get("span_id") or ""),
        name=name,
        parent_id=str(item.get("parent_span_id") or ""),
        status=str(item.get("status") or "ok"),
        error_code=str(item.get("error_code") or ""),
        duration_ms=duration_ms,
        attributes=attributes,
    )


def waterfall(spans: Sequence[SpanRecord]) -> list[dict[str, Any]]:
    """按开始时间排出瀑布：每条给出相对于运行开始的偏移、耗时和层级深度。

    偏移和耗时都是真数值（来自 OTel 时间戳）。没有真实时间的 span 会在
    trace_shape 里被标成 duration_ms=0，这里如实照抄，不补一个看起来合理的数。
    """
    if not spans:
        return []
    starts = {span.span_id: int(dict(span.attributes).get("__start_ns__") or 0) for span in spans}
    base = min([value for value in starts.values() if value > 0] or [0])
    by_id = {span.span_id: span for span in spans}

    def depth_of(span: SpanRecord, seen: int = 0) -> int:
        parent = str(span.parent_id or "")
        if not parent or parent not in by_id or seen > 32:
            return 0
        return 1 + depth_of(by_id[parent], seen + 1)

    rows: list[dict[str, Any]] = []
    for span in spans:
        start = starts.get(span.span_id, 0)
        rows.append(
            {
                "span_id": span.span_id,
                "parent_span_id": span.parent_id,
                "name": span.name,
                "depth": depth_of(span),
                "start_offset_ms": round((start - base) / 1_000_000.0, 3) if start and base else 0.0,
                "duration_ms": float(span.duration_ms),
                "status": span.status,
                "error_code": span.error_code,
            }
        )
    return rows


def _iso_from_ns(value: Any) -> str:
    """把 span 的纳秒起点转成 ISO 时间。转不出来返回空串，不拿「现在」顶替。

    为什么不用当前时间兜底：日志的时间戳一旦被写成「读这份数据的那一刻」，
    一次昨天跑的运行在界面上就会显示成今天，而排序、对时、找因果全靠这个字段。
    宁可空着让人看见「这条没有时间」，也不要给一个看着正常的假时间。
    """
    try:
        ns = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if ns <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ns / 1_000_000_000.0, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def logs_from_spans(
    spans: Sequence[Mapping[str, Any]],
    *,
    trace_id: str,
    thread_id: str,
    secrets: Sequence[str] = (),
) -> list[LogRecord]:
    """用真 span 生成结构化日志：trace_id / span_id 全部取自 span 本身。

    为什么不在这里编一句「工具调用成功」：那样日志就和 span 脱钩了，
    看起来有条日志、实际上没人能证明它对应哪次调用。这里的 message 只由
    span 的名字、状态和错误码拼出来，信息量少但没有一个字是编的。
    """
    records: list[LogRecord] = []
    for item in spans:
        name = str(item.get("name") or "")
        event = SPAN_TO_LOG_EVENT.get(name)
        if not event:
            continue
        status = str(item.get("status") or "ok")
        error_code = str(item.get("error_code") or "")
        message = name + " " + status
        if error_code:
            message = message + " " + error_code
        # 时间戳取自 span 自己的起点，不取「读数据的时间」：
        # 否则一次昨天跑的运行会在界面上显示成今天。
        record = build_log_record(
            trace_id=trace_id,
            event=event,
            severity="error" if status == "error" else "info",
            message=message,
            span_id=str(item.get("span_id") or ""),
            parent_span_id=str(item.get("parent_span_id") or ""),
            thread_id=thread_id,
            occurred_at=_iso_from_ns(item.get("start_ns")),
            # 不传 attributes：它不在日志字段白名单里，而且是个字典。
            # span 的名字已经写在 event 与 message 里，不需要再塞一份容器字段。
            secrets=secrets,
        )
        records.append(record)
    return records


@dataclass(frozen=True)
class TraceDetail:
    """一次运行的完整 trace 明细。所有字段都能直接 json.dumps。"""

    # trace_id 是本服务的运行 trace 编号（tr 开头）；otel_trace_id 是 span 上那个
    # 32 位十六进制 id。两者都留着，因为查 SkyWalking 得用后者。
    trace_id: str
    otel_trace_id: str
    thread_id: str
    run_id: str
    shape: TraceShape
    waterfall: list[dict[str, Any]]
    logs: list[LogRecord]
    log_report: Any
    metrics: MetricBatch
    alerts: AlertEvaluation
    links: list[LinkRecord]
    unmapped_spans: int
    recorder_dropped: int
    schema_version: str = TRACE_DETAIL_SCHEMA_VERSION

    @property
    def ok(self) -> bool:
        """明细自身是否可信：层级无错、日志关联无错、告警判定有结论。"""
        return not self.shape.hierarchy_errors() and bool(self.log_report.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "otel_trace_id": self.otel_trace_id,
            "thread_id": self.thread_id,
            "run_id": self.run_id,
            "ok": bool(self.ok),
            "unmapped_spans": int(self.unmapped_spans),
            "recorder_dropped": int(self.recorder_dropped),
            "shape": self.shape.to_dict(),
            "hierarchy_errors": list(self.shape.hierarchy_errors()),
            "waterfall": list(self.waterfall),
            "logs": [item.to_dict() for item in self.logs],
            "log_report": self.log_report.to_dict(),
            "metrics": self.metrics.to_dict(),
            "alerts": self.alerts.to_dict(),
            "links": [item.to_dict() for item in self.links],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def build_trace_detail(
    *,
    trace_id: str,
    otel_trace_id: str = "",
    thread_id: str,
    run_id: str = "",
    spans: Sequence[Mapping[str, Any]],
    metrics: MetricBatch,
    alerts: AlertEvaluation,
    links: Sequence[LinkRecord] = (),
    interrupted: bool = False,
    resumed_from: str = "",
    secrets: Sequence[str] = (),
    recorder_dropped: int = 0,
) -> TraceDetail:
    """把「一次运行的所有 span」组装成明细。

    metrics 与 alerts 必须由调用方传进来：它们是全量状态（可能涵盖多次运行），
    在这里现算会得出和落盘文件不一致的第二份数字。
    """
    if not str(trace_id or ""):
        raise ContractError(ERROR_TRACE_RECORD, "trace_detail 需要非空 trace_id")
    tid = str(thread_id or "")
    # span 上的 trace_id 是 OTel 的 32 位十六进制 id，本服务的 trace_id 是运行编号，
    # 两者不是同一个东西。按运行编号筛会把所有 span 都筛掉，而且看起来像
    # 「这次运行一个 span 都没有」——那是最难查的一类假象，所以这里必须显式区分：
    # 给了 otel_trace_id 就按它筛；没给就认为调用方已经把 span 限定到本次运行了。
    wanted = str(otel_trace_id or "")
    if wanted:
        rows = [item for item in spans if str(item.get("trace_id") or "") == wanted]
    else:
        rows = list(spans)
    records: list[SpanRecord] = []
    unmapped = 0
    for item in rows:
        record = _to_span_record(item)
        if record is None:
            unmapped += 1
            continue
        attributes = dict(record.attributes)
        attributes["__start_ns__"] = int(item.get("start_ns") or 0)
        records.append(
            SpanRecord(
                span_id=record.span_id,
                name=record.name,
                parent_id=record.parent_id,
                status=record.status,
                error_code=record.error_code,
                duration_ms=record.duration_ms,
                attributes=attributes,
            )
        )
    records.sort(key=lambda item: int(dict(item.attributes).get("__start_ns__") or 0))
    shape = trace_shape(
        trace_id=str(trace_id),
        spans=records,
        links=tuple(links),
        interrupted=bool(interrupted),
        resumed_from=str(resumed_from or ""),
    )
    log_rows = logs_from_spans(rows, trace_id=str(trace_id), thread_id=tid, secrets=secrets)
    known = {item.span_id for item in records}
    log_report = correlate_logs(log_rows, trace_id=str(trace_id), known_span_ids=known)
    return TraceDetail(
        trace_id=str(trace_id),
        otel_trace_id=wanted,
        thread_id=tid,
        run_id=str(run_id or ""),
        shape=shape,
        waterfall=waterfall(records),
        logs=log_rows,
        log_report=log_report,
        metrics=metrics,
        alerts=alerts,
        links=list(links),
        unmapped_spans=int(unmapped),
        recorder_dropped=int(recorder_dropped),
    )


def resume_links(
    *,
    thread_id: str,
    new_trace_id: str,
    prior_trace_ids: Iterable[str],
    approval_id: str = "",
) -> list[LinkRecord]:
    """中断—恢复之间的因果链。

    为什么必须有这一步：SkyWalking 里两个 trace 长得一模一样，没有 link 就无法
    回答「这次恢复到底是接着哪次中断跑的」。links 只在确实存在上一段 trace 时才建，
    首次运行不会凭空造一条指向空气的链接。
    """
    links: list[LinkRecord] = []
    for prior in prior_trace_ids:
        text = str(prior or "")
        if not text or text == str(new_trace_id):
            continue
        links.append(
            LinkRecord(
                from_trace_id=str(new_trace_id),
                to_trace_id=text,
                reason="resume_after_interrupt",
                thread_id=str(thread_id),
                approval_id=str(approval_id or ""),
            )
        )
    return links


def _duration_ms(row: Mapping[str, Any]) -> int:
    """从 start_ns / end_ns 算耗时。缺时间戳就返回 0，不估算。"""
    try:
        start = int(row.get("start_ns") or 0)
        end = int(row.get("end_ns") or 0)
    except (TypeError, ValueError):
        return 0
    if start <= 0 or end <= start:
        return 0
    return int(round((end - start) / 1_000_000.0))


def _as_bool(value: Any, *, default: bool) -> bool:
    """把 span 属性里的布尔还原出来。属性经过序列化可能是字符串，识别不了就用默认值。"""
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return bool(default)


def run_facts_from_spans(
    spans: Iterable[Mapping[str, Any]],
    *,
    intent: str = "",
    intent_source: str = "",
    mode: str = "",
    duration_ms: int = 0,
    rag_hits: int = 0,
    approval_backlog: int = 0,
    exporter: str = "",
    telemetry_degraded: bool = False,
    telemetry_dropped: int = 0,
    usage_status: str = USAGE_STATUS_UNKNOWN,
) -> dict[str, Any]:
    """把一条 trace 的 span 折算成 collect_run_metrics 需要的结构化事实。

    为什么要有这一步：collect_run_metrics 吃的是「运行事实」，不吃 span 对象。
    事实必须来自真 span 上带的属性，不在这里替 span 猜数值——
    rag_hits 就明确不从 span 推，它只能由调用方给出真实命中数，
    用 rag.top_k 冒充命中数会让「检索到几条」这个数字变成假的。

    没有 run span 时 duration_ms 保持入参，不按节点耗时拼一个出来。
    """
    rows = [item for item in spans or () if isinstance(item, Mapping)]
    node_durations: dict[str, int] = {}
    tool_calls: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    sandbox_calls: list[dict[str, Any]] = []
    error_codes: set[str] = set()
    pending = 0
    resolved_mode = str(mode or "")
    resolved_duration = max(0, int(duration_ms or 0))
    for row in rows:
        name = str(row.get("name") or "")
        attrs = dict(row.get("attributes") or {})
        status = str(row.get("status") or "ok")
        span_ms = _duration_ms(row)
        if status == "error":
            error_codes.add(str(row.get("error_code") or "UNKNOWN"))
        if name == SPAN_RUN:
            if not resolved_mode:
                resolved_mode = str(attrs.get("agent.mode") or "")
            if not resolved_duration:
                resolved_duration = span_ms
        elif name == SPAN_NODE:
            node = str(attrs.get("agent.node") or "")
            if node:
                node_durations[node] = node_durations.get(node, 0) + span_ms
        elif name == SPAN_TOOL:
            tool_calls.append({
                "tool_name": str(attrs.get("tool.name") or "unknown"),
                "capability": str(attrs.get("tool.capability") or "none"),
                "ok": _as_bool(attrs.get("tool.ok"), default=status != "error"),
                "dry_run": _as_bool(attrs.get("tool.dry_run"), default=True),
                "duration_ms": span_ms,
            })
        elif name == SPAN_APPROVAL:
            decision = str(attrs.get("approval.decision") or "")
            if not decision:
                pending += 1
            approvals.append({
                "status": decision or "pending",
                "tool_name": str(attrs.get("approval.tool") or "unknown"),
                "capability": str(attrs.get("tool.capability") or "none"),
            })
        elif name == SPAN_SANDBOX:
            sandbox_calls.append({
                "runtime": str(attrs.get("runtime") or "unknown"),
                "isolation_level": str(attrs.get("isolation_level") or "unknown"),
                "status": str(attrs.get("sandbox.status") or status),
                "duration_ms": span_ms,
            })
    return {
        "intent": str(intent or ""),
        "intent_source": str(intent_source or ""),
        "mode": resolved_mode,
        "duration_ms": resolved_duration,
        "node_durations": node_durations,
        "error_codes": tuple(sorted(error_codes)),
        "tool_calls": tool_calls,
        "approvals": approvals,
        "approval_backlog": max(int(approval_backlog or 0), pending),
        "sandbox_calls": sandbox_calls,
        "rag_hits": max(0, int(rag_hits or 0)),
        "telemetry_dropped": max(0, int(telemetry_dropped or 0)),
        "telemetry_degraded": bool(telemetry_degraded),
        "exporter": str(exporter or ""),
        "usage_status": str(usage_status or USAGE_STATUS_UNKNOWN),
    }


def detail_report(detail: TraceDetail) -> dict[str, Any]:
    """人看的摘要：给 doctor / 命令行用，不替代完整明细。"""
    return {
        "trace_id": detail.trace_id,
        "thread_id": detail.thread_id,
        "run_id": detail.run_id,
        "ok": bool(detail.ok),
        "complete": bool(detail.shape.complete),
        "span_count": len(detail.shape.spans),
        "missing_spans": list(detail.shape.missing_spans()),
        "hierarchy_errors": list(detail.shape.hierarchy_errors()),
        "log_count": len(detail.logs),
        "log_ok": bool(detail.log_report.ok),
        "alert_count": len(detail.alerts.firing),
        "metric_count": len(detail.metrics.metrics),
        "link_count": len(detail.links),
        "unmapped_spans": int(detail.unmapped_spans),
        "alert_rules": len(ALERT_RULES),
    }


__all__ = [
    "TRACE_DETAIL_SCHEMA_VERSION",
    "ERROR_TRACE_RECORD",
    "MAX_RECORDED_SPANS",
    "SPAN_TO_LOG_EVENT",
    "TraceRecordingExporter",
    "TraceDetail",
    "build_trace_detail",
    "waterfall",
    "logs_from_spans",
    "resume_links",
    "run_facts_from_spans",
    "detail_report",
]
