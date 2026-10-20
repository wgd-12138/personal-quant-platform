"""A1-06 telemetry 脱敏：把 pq_agent.security 的闸门套在 span 属性 / 事件 / exporter 上。

为什么不能只靠"记得不要写敏感字段"：

1. 属性值可能来自工具返回、异常文本、用户输入，任何一个都可能夹带 webhook 或 token；
2. OTel 的 exporter 会把属性原样序列化并发出去，中间没有第二道闸门；
3. 因此**出口**必须再拦一次。入口的键名黑名单只能挡住"明知道不该写的"，挡不住"意外带进来的"。

本模块不重新实现脱敏规则，全部复用 pq_agent.security.redaction。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pq_agent.contracts import ContractError
from pq_agent.security.redaction import (
    DEFAULT_MAX_TEXT_LEN,
    find_secrets,
    guard_payload,
    is_sensitive_key,
    scrub_text,
    scrub_value,
)

TELEMETRY_SCHEMA_VERSION = 1

# span 允许出现的属性名。白名单以外的一律丢弃并记一条降级事件——
# 企业里真正危险的不是"少了一个属性"，而是"悄悄多了一个带密钥的属性"。
SPAN_ATTRIBUTE_ALLOWLIST: tuple[str, ...] = (
    "agent.intent",
    "agent.intent_source",
    "agent.mode",
    "agent.node",
    "agent.turn",
    "as_of",
    "data_snapshot_id",
    "degraded",
    "duration_ms",
    "error_code",
    "error_type",
    "graph_version",
    "status",
    "thread_id",
    "tool.capability",
    "tool.dry_run",
    "tool.name",
    "tool.ok",
    "tool.result_count",
    "trace_id",
    "usage.cost",
    "usage.cost_status",
    "usage.tokens",
    "usage.tokens_status",
    # ---- A4-02：三信号与拓扑需要的属性
    # 全部是枚举型或低基数标签。这里每加一个名字都要问一句：
    # 「它的取值集合有限吗？」——无限取值的东西加进 span 属性，
    # 和加进 metrics label 一样会炸后端，只是炸得晚一点。
    "runtime",
    "isolation_level",
    "sandbox.status",
    "approval.decision",
    "approval.tool",
    "rag.mode",
    "rag.top_k",
    "llm.source",
    "llm.model",
    "link.resumed_from",
    "link.reason",
)

# 这些键名即使出现在白名单里也必须直接拒绝（防手滑把 prompt 加进白名单）
TRACING_FORBIDDEN_ATTRIBUTE_KEYS: tuple[str, ...] = (
    "prompt",
    "system_prompt",
    "raw_prompt",
    "messages",
    "message_content",
    "headers",
    "authorization",
    "cookie",
    "webhook",
    "webhook_url",
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "checkpoint",
    "exception",
    "stack",
    "traceback",
    "file_content",
    "arguments",
)

# 允许进入 telemetry 的标量类型；其余一律转成类型名，绝不 repr
_SCALARS = (str, int, float, bool)


class TelemetryRedactionError(ContractError):
    """telemetry 脱敏失败。继承 ContractError 便于沿用错误码断言。"""


def is_sensitive_attribute(key: str) -> bool:
    """键名判定：命中黑名单或命中 security 层的敏感键名规则都算。"""
    text = str(key).strip()
    if not text:
        return True
    lowered = text.lower()
    for forbidden in TRACING_FORBIDDEN_ATTRIBUTE_KEYS:
        if lowered == forbidden:
            return True
    if is_sensitive_key(text):
        return True
    return False


def _is_allowed(key: str) -> bool:
    return str(key) in SPAN_ATTRIBUTE_ALLOWLIST


def _coerce_scalar(value: Any) -> Any:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value
    return f"<{type(value).__name__}>"


class TelemetryRedactor:
    """telemetry 出口脱敏器。secrets 由调用方登记（来自环境变量收集），本类不读环境。"""

    def __init__(self, *, secrets: Iterable[str] | None = None, drop_unknown_keys: bool = True) -> None:
        self._secrets = tuple(s for s in (secrets or ()) if isinstance(s, str) and s.strip())
        self._drop_unknown_keys = bool(drop_unknown_keys)

    @property
    def secrets(self) -> tuple[str, ...]:
        return self._secrets

    def sanitize(self, key: str, value: Any) -> tuple[bool, Any, str]:
        """返回 (是否保留, 值, 丢弃原因)。丢弃原因用于计数，不写进 span。"""
        text = str(key)
        if is_sensitive_attribute(text):
            return False, None, "forbidden_key"
        if not _is_allowed(text) and self._drop_unknown_keys:
            return False, None, "not_allowlisted"
        scalar = _coerce_scalar(value)
        if isinstance(scalar, str):
            try:
                cleaned = scrub_text(scalar, secrets=self._secrets, max_len=DEFAULT_MAX_TEXT_LEN)
            except Exception as exc:
                raise TelemetryRedactionError(
                    "TELEMETRY_REDACTION_FAILED",
                    f"属性 {text} 脱敏失败：{type(exc).__name__}",
                ) from exc
            return True, cleaned, ""
        return True, scalar, ""

    def sanitize_attributes(self, attributes: Mapping[str, Any] | None) -> dict[str, Any]:
        """批量脱敏；返回的字典可以直接挂到 span 上。"""
        out: dict[str, Any] = {}
        for key, value in (attributes or {}).items():
            keep, cleaned, _reason = self.sanitize(key, value)
            if keep:
                out[str(key)] = cleaned
        return out

    def sanitize_payload(self, payload: Any, *, where: str = "telemetry") -> Any:
        """结构化载荷（事件属性、本地事件）走 security 的整包闸门。"""
        return guard_payload(payload, secrets=self._secrets, where=where)


def sanitize_attributes(
    attributes: Mapping[str, Any] | None,
    *,
    secrets: Iterable[str] | None = None,
    drop_unknown_keys: bool = True,
) -> dict[str, Any]:
    """无状态入口：给不便持有 redactor 的调用方（如 exporter）用。"""
    redactor = TelemetryRedactor(secrets=secrets, drop_unknown_keys=drop_unknown_keys)
    return redactor.sanitize_attributes(attributes)


def sanitize_event_payload(payload: Any, *, secrets: Iterable[str] | None = None, where: str = "telemetry.event") -> Any:
    """事件载荷脱敏；这里用 scrub_value 而不是 guard_payload，
    因为事件载荷里允许出现 unknown / 数字等非字符串标量，
    而 guard_payload 的残留自检只针对字符串，两者并不冲突。
    但只要有残留就抛错——宁可丢事件，也不让未脱敏内容出门。
    """
    try:
        cleaned = scrub_value(payload, secrets=secrets)
    except Exception as exc:
        raise TelemetryRedactionError(
            "TELEMETRY_REDACTION_FAILED",
            f"{where} 脱敏失败：{type(exc).__name__}",
        ) from exc
    leaked = find_secrets(cleaned, secrets=secrets)
    if leaked:
        raise TelemetryRedactionError(
            "TELEMETRY_SECRET_LEAK_BLOCKED",
            f"{where}: {sorted(set(leaked))}",
        )
    return cleaned


class SanitizingSpanExporter:
    """把脱敏闸门装在 exporter 出口上的包装器。

    为什么要有这一层：OTel 的属性是在 span 创建时写进去的，但 exporter 会把 span
    原样序列化后发到进程之外——中间没有任何人再检查一次。企业里最常见的 trace 事故
    就是"某个中间件偷偷把 header 或 URL 写进了 span"，然后随着 trace 一起出了网。
    所以这里在**出口**再拦一次：属性被重新过一遍白名单与脱敏，事件载荷被整体扫描，
    发现残留宁可丢属性也不发出去。
    """

    def __init__(self, inner: Any, *, secrets: Iterable[str] | None = None) -> None:
        self._inner = inner
        self._redactor = TelemetryRedactor(secrets=secrets)
        self.dropped_attributes = 0

    @property
    def inner(self) -> Any:
        return self._inner

    @property
    def redactor(self) -> TelemetryRedactor:
        return self._redactor

    def export(self, spans: Sequence[Any]):
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            for span in spans:
                self._sanitize_span(span)
        except Exception:
            return SpanExportResult.FAILURE
        return self._inner.export(list(spans))

    def _sanitize_span(self, span: Any) -> None:
        attributes = getattr(span, "attributes", None)
        if attributes is None:
            return
        cleaned: dict[str, Any] = {}
        for key, value in list(attributes.items()):
            keep, new_value, _reason = self._redactor.sanitize(key, value)
            if keep:
                cleaned[key] = new_value
            else:
                self.dropped_attributes += 1
        try:
            span._attributes = cleaned
        except Exception:
            for key in list(attributes.keys()):
                try:
                    del attributes[key]
                except Exception:
                    pass
            try:
                attributes.update(cleaned)
            except Exception:
                pass

    def shutdown(self) -> None:
        return self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        method = getattr(self._inner, "force_flush", None)
        if method is None:
            return True
        return bool(method(timeout_millis))

def exporter_configuration(
    *,
    endpoint: str | None = None,
    protocol: str = "http/protobuf",
    timeout_s: float = 5.0,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """描述一次 exporter 配置，不负责真正构造——构造在 tracing 模块里。

    这里刻意把 headers 是否非空记成布尔而不是值：
    Collector 的鉴权头本身就是密钥。
    """
    return {
        "endpoint": endpoint or "",
        "protocol": protocol,
        "timeout_s": float(timeout_s),
        "has_headers": bool(headers),
        "header_configured": "yes" if headers else "no",
    }


class SpinCallbackExporter:
    """只收集、不外发的 exporter，供单测与无 Collector 环境使用。

    它不是"假的 OTLP"：它是一个**明确的本地扇出点**，
    真正的 OTLP exporter 与它实现同一个 SpanExporter 协议。
    """

    def __init__(self) -> None:
        self.spans: list[Any] = []
        self.shutdown_called = False

    def export(self, spans: Sequence[Any]) -> int:
        try:
            for span in spans:
                self.spans.append(span)
        except Exception:
            from opentelemetry.sdk.trace.export import SpanExportResult

            return SpanExportResult.FAILURE
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        self.shutdown_called = True

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True
